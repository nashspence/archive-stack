from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import jeb.collector as collector_module
from jeb.collector import (
    Collector,
    EligibleFile,
    NotifySettings,
    WebhookNotifier,
    config_from_env,
    munchy_upload_request,
    parse_duration,
    parse_size,
)


def env_for(tmp_path: Path, *, accounts: str = "camera,phone") -> dict[str, str]:
    return {
        "JEB_ACCOUNTS": accounts,
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://runner.test",
        "JEB_INCLUDE_EXTENSIONS": ".txt,.mp4",
        "JEB_STABLE_AGE": "0s",
        "JEB_CADENCE": "weekly",
        "JEB_ARCHIVE_TASKS": "archive_video",
    }


def write_munchy_config(tmp_path: Path, config: dict[str, object]) -> str:
    path = tmp_path / "munchy-job.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    return str(path)


def write_stable_file(path: Path, content: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def active_attempt_ids(collector: Collector) -> list[str]:
    return [str(row["id"]) for row in collector.active_attempts()]


@dataclass
class CompleteRunner:
    calls: int = 0

    def advance(self, collector: Collector, attempt_id: str) -> None:
        self.calls += 1
        collector.set_attempt_state(attempt_id, "target_complete")


@dataclass
class RecordingNotifier:
    calls: list[dict[str, object]]

    def issue(
        self,
        *,
        context,
        message,
        component,
        severity,
        notify=None,
    ) -> bool:
        self.calls.append(
            {
                "context": dict(context),
                "message": message,
                "component": component,
                "severity": severity,
                "notify": dict(notify or {}),
            }
        )
        return True


def test_parse_helpers_support_human_units() -> None:
    assert parse_duration("10m") == 600
    assert parse_duration("2h") == 7200
    assert parse_size("2GiB") == 2 * 1024**3


def test_env_config_creates_accounts(tmp_path: Path) -> None:
    config = config_from_env(env_for(tmp_path))

    assert [account.id for account in config.accounts] == ["camera", "phone"]
    assert [str(account.path) for account in config.accounts] == [
        str(tmp_path / "landing" / "camera"),
        str(tmp_path / "landing" / "phone"),
    ]
    assert config.collector.batch_dir == tmp_path / "landing" / ".jeb-batches"
    account_keys = [(account.id, account.collection_slug) for account in config.accounts]
    assert account_keys == [
        ("camera", "camera"),
        ("phone", "phone"),
    ]
    assert config.munchy_job_defaults["tasks"] == ["archive_video"]
    assert config.munchy_job_defaults["collection_archive"]["riverhog"] == {
        "wait": "finalized",
        "retain_hot": True,
    }


def test_jeb_schema_indexes_operator_status_and_list_paths(tmp_path: Path) -> None:
    collector = Collector(config_from_env(env_for(tmp_path)))
    collector.init_db()

    with collector.connect() as conn:
        indexes = {
            table: {str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")}
            for table in (
                "batches",
                "batch_attempts",
                "files",
                "attempt_files",
            )
        }
        batch_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(batches)")}
        triggers = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }

    assert {"file_count", "total_bytes"} <= batch_columns
    assert {
        "idx_jeb_batches_account",
        "idx_jeb_batches_account_period",
        "idx_jeb_batches_file_count",
        "idx_jeb_batches_target",
        "idx_jeb_batches_total_bytes",
    } <= indexes["batches"]
    assert {
        "idx_jeb_batch_attempts_batch_state",
        "idx_jeb_batch_attempts_created",
        "idx_jeb_batch_attempts_job",
        "idx_jeb_batch_attempts_state",
        "idx_jeb_batch_attempts_state_updated",
        "idx_jeb_batch_attempts_updated",
    } <= indexes["batch_attempts"]
    assert "idx_jeb_files_batch" in indexes["files"]
    assert "idx_jeb_attempt_files_attempt" in indexes["attempt_files"]
    assert {
        "trg_jeb_files_summary_delete",
        "trg_jeb_files_summary_insert",
        "trg_jeb_files_summary_update_moved_batch",
        "trg_jeb_files_summary_update_same_batch",
    } <= triggers


def test_jeb_batch_file_summaries_are_trigger_maintained_transactionally(
    tmp_path: Path,
) -> None:
    env = env_for(tmp_path, accounts="camera")
    write_stable_file(tmp_path / "landing" / "camera" / "a.txt", b"aaa")
    write_stable_file(tmp_path / "landing" / "camera" / "b.txt", b"bbbbb")
    collector = Collector(config_from_env(env))
    collector.init_db()
    batch_id = collector.archive_now(account_id="camera", process=False)
    assert batch_id is not None

    page = collector.list_attempts(terminal="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 2
    assert page["attempts"][0]["total_bytes"] == 8

    with pytest.raises(RuntimeError):
        with collector.connect() as conn:
            conn.execute(
                "UPDATE files SET bytes = 99 WHERE batch_id = ? AND target_path = ?",
                (batch_id, "camera/a.txt"),
            )
            raise RuntimeError("rollback")

    page = collector.list_attempts(terminal="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 2
    assert page["attempts"][0]["total_bytes"] == 8

    with collector.connect() as conn:
        conn.execute(
            "UPDATE files SET bytes = 7 WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/a.txt"),
        )

    page = collector.list_attempts(terminal="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 2
    assert page["attempts"][0]["total_bytes"] == 12

    with collector.connect() as conn:
        conn.execute(
            "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/b.txt"),
        )

    page = collector.list_attempts(terminal="all", sort="bytes")
    assert page["attempts"][0]["file_count"] == 1
    assert page["attempts"][0]["total_bytes"] == 7

    assert collector.list_attempts(terminal="all", account="camera")["total"] == 1

    with collector.connect() as conn:
        conn.execute(
            "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/a.txt"),
        )

    page = collector.list_attempts(terminal="all", sort="bytes")
    assert page["attempts"][0]["account_id"] == "camera"
    assert page["attempts"][0]["file_count"] == 0
    assert page["attempts"][0]["total_bytes"] == 0
    assert collector.list_attempts(terminal="all", account="camera")["total"] == 1


def test_env_config_supports_per_account_munchy_config_file(
    tmp_path: Path,
) -> None:
    munchy_config = {
        "schema_version": 1,
        "kind": "munchy.job",
        "job": {
            "workflow_mode": "collection_archive",
            "routing": {
                "routes": [
                    {
                        "id": "main-video",
                        "group": "camera-video",
                        "when": {"path": {"prefix": "camera"}},
                    }
                ]
            },
        },
        "groups": {
            "camera-video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
                "metadata_projection": {
                    "gps": {"latitude": 48.9995, "longitude": -122.7404},
                },
            }
        },
    }
    env = {
        **env_for(tmp_path, accounts="camera"),
        "JEB_ACCOUNT_CAMERA_MUNCHY_CONFIG": write_munchy_config(tmp_path, munchy_config),
    }

    config = config_from_env(env)

    assert config.accounts[0].upload_root == "camera"
    defaults = dict(config.accounts[0].munchy_job_defaults)
    assert defaults["workflow_mode"] == "collection_archive"
    assert defaults["groups"] == munchy_config["groups"]
    assert defaults["routing"]["routes"][0]["group"] == "camera-video"


def test_env_config_supports_account_munchy_config_dir(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "munchy"
    config_dir.mkdir()
    write_munchy_config(
        config_dir,
        {
            "schema_version": 1,
            "kind": "munchy.job",
            "job": {"workflow_mode": "collection_archive"},
            "groups": {"camera-video": {"output_mode": "video"}},
        },
    )
    (config_dir / "munchy-job.yaml").rename(config_dir / "camera.munchy.yaml")

    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_MUNCHY_CONFIG_DIR": str(config_dir),
        }
    )

    assert config.accounts[0].munchy_job_defaults["groups"] == {
        "camera-video": {
            "output_mode": "video",
            "tasks": ["archive_video", "qcut_video", "audio_review"],
        }
    }


def test_env_config_supports_per_account_cadence(tmp_path: Path) -> None:
    env = {
        **env_for(tmp_path, accounts="front-door,phone"),
        "JEB_CADENCE": "monthly",
        "JEB_ACCOUNT_FRONT_DOOR_CADENCE": "seasonal",
        "JEB_ACCOUNT_PHONE_CADENCE": "manual",
    }

    config = config_from_env(env)

    assert [collection.cadence for collection in config.accounts] == [
        "seasonal",
        "manual",
    ]


def test_env_config_supports_per_account_munchy_notify(tmp_path: Path) -> None:
    env = {
        **env_for(tmp_path, accounts="front-door,phone"),
        "JEB_NOTIFY_ENABLED": "true",
        "JEB_NOTIFY_RECIPIENTS": "operator",
        "JEB_ACCOUNT_FRONT_DOOR_NOTIFY_RECIPIENTS": "operator,collaborator",
        "JEB_ACCOUNT_PHONE_NOTIFY_ENABLED": "false",
        "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
    }

    config = config_from_env(env)

    assert [collection.notify for collection in config.accounts] == [
        {"enabled": True, "recipients": ["operator", "collaborator"]},
        {"enabled": False, "recipients": ["operator"]},
    ]
    assert config.notify.webhook_urls == {}


def test_env_config_loads_named_notify_webhook_map(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "operator,collaborator",
            "RIVERHOG_COLLECTION_WEBHOOKS": '{"operator":"http://operator.test","collaborator":"http://collaborator.test"}',
        }
    )

    assert config.notify.webhook_urls == {
        "operator": "http://operator.test",
        "collaborator": "http://collaborator.test",
    }


def test_env_config_rejects_invalid_cadence(tmp_path: Path) -> None:
    env = {**env_for(tmp_path), "JEB_CADENCE": "daily"}

    with pytest.raises(ValueError, match="JEB_CADENCE"):
        config_from_env(env)


def test_env_config_requires_safe_account_slugs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="safe slug"):
        config_from_env(env_for(tmp_path, accounts="camera,phone/raw"))


def test_cleanup_after_success_requires_safe_munchy_target(tmp_path: Path) -> None:
    env = {
        **env_for(tmp_path),
        "JEB_CLEANUP": "after_target_success",
        "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE": "false",
    }

    with pytest.raises(ValueError, match="safe-delete"):
        config_from_env(env)


def test_scheduler_batches_each_account_independently(tmp_path: Path) -> None:
    config = config_from_env(env_for(tmp_path))
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    runner = CompleteRunner()
    collector = Collector(config, target_runners={"munchy": runner})

    collector.run_once()
    assert len(active_attempt_ids(collector)) == 2
    collector.run_once()

    assert runner.calls == 2
    with collector.connect() as conn:
        batches = conn.execute(
            "SELECT account_id, collection_slug FROM batches ORDER BY account_id"
        ).fetchall()
    assert sorted({(row["account_id"], row["collection_slug"]) for row in batches}) == [
        ("camera", "camera"),
        ("phone", "phone"),
    ]


def test_manual_cadence_only_runs_with_archive_now(tmp_path: Path) -> None:
    config = config_from_env({**env_for(tmp_path, accounts="camera"), "JEB_CADENCE": "manual"})
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    collector = Collector(config)

    collector.run_once()
    assert active_attempt_ids(collector) == []

    assert collector.archive_now(account_id="camera", process=False) is not None
    assert len(active_attempt_ids(collector)) == 1


def test_archive_plan_reports_batch_without_creating_it(tmp_path: Path) -> None:
    config = config_from_env(env_for(tmp_path, accounts="camera"))
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt", b"camera")
    collector = Collector(config)
    collector.init_db()

    plan = collector.archive_plan(account_id="camera")

    assert plan["status"] == "would_process"
    assert plan["dry_run"] is True
    assert plan["account"] == "camera"
    assert plan["collection_slug"] == "camera"
    assert plan["target_name"] == "munchy"
    assert plan["file_count"] == 1
    assert plan["total_bytes"] == 6
    assert plan["batch_id"]
    assert plan["job_id"]
    assert active_attempt_ids(collector) == []


def test_archive_plan_routing_preflight_does_not_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    munchy_config = {
        "schema_version": 1,
        "kind": "munchy.job",
        "job": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "routing": {
                "routes": [
                    {
                        "id": "main-video",
                        "group": "camera-video",
                        "when": {"path": {"prefix": "camera"}},
                    }
                ]
            },
        },
        "groups": {
            "camera-video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            }
        },
    }
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_ACCOUNT_CAMERA_MUNCHY_CONFIG": write_munchy_config(tmp_path, munchy_config),
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    collector = Collector(config)
    collector.init_db()

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def routing_preflight(self, **_kwargs: object) -> dict[str, object]:
            return {
                "ok": False,
                "matches": [],
                "unmatched": [{"path": "camera/clip.mp4", "reason": "no_matching_route"}],
            }

    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    plan = collector.archive_plan(account_id="camera")

    assert plan["status"] == "routing_preflight_failed"
    assert plan["routing_preflight"]["unmatched_count"] == 1
    assert active_attempt_ids(collector) == []
    assert collector.routing_preflight_failures(state="failed") == []


def test_monthly_cadence_uses_first_scheduled_run_after_month_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="phone"),
            "JEB_CADENCE": "monthly",
            "JEB_WEEKDAY": "monday",
            "JEB_HOUR": "3",
            "JEB_MINUTE": "0",
        }
    )
    collector = Collector(config)
    [collection] = config.accounts

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert collector.account_period(collection) == datetime(2026, 7, 6, 3, tzinfo=UTC)

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 7, 2, 12, tzinfo=UTC))
    assert collector.account_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)


def test_seasonal_cadence_uses_first_scheduled_run_after_custom_season_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="phone"),
            "JEB_CADENCE": "seasonal",
            "JEB_WEEKDAY": "monday",
            "JEB_HOUR": "3",
            "JEB_MINUTE": "0",
        }
    )
    collector = Collector(config)
    [collection] = config.accounts

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert collector.account_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 6, 1, 2, tzinfo=UTC))
    assert collector.account_period(collection) == datetime(2026, 3, 2, 3, tzinfo=UTC)


def test_munchy_payload_uses_account_group_paths_without_routing(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "operator",
            "JEB_ACCOUNT_CAMERA_NOTIFY_RECIPIENTS": "operator,collaborator",
            "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    collector = Collector(config)
    collector.init_db()

    batch_id = collector.archive_now(account_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(collector, batch_id, config.targets["munchy"])

    assert [item.rel_path for item in request.files] == ["camera/clip.txt"]
    assert request.storage_hint == {
        "workflow_mode": "collection_archive",
        "collection_archive_destination": "riverhog",
        "output_mode": "video",
        "tasks": ["archive_video"],
        "structured_routing": False,
        "groups": {},
    }
    assert request.job_payload["collection_slug"] == "camera"
    assert request.job_payload["tasks"] == ["archive_video"]
    assert request.job_payload["groups"] == {}
    assert request.job_payload["notify"] == {
        "enabled": True,
        "recipients": ["operator", "collaborator"],
    }
    assert request.job_payload["riverhog_upload_session_on_failure"] == "cancel"
    assert "routing" not in request.job_payload


def test_munchy_payload_uses_per_account_job_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    munchy_config = {
        "schema_version": 1,
        "kind": "munchy.job",
        "job": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "routing": {
                "routes": [
                    {
                        "id": "main-video",
                        "group": "camera-video",
                        "when": {"path": {"prefix": "camera"}},
                    }
                ]
            },
        },
        "groups": {
            "camera-video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
                "metadata_projection": {
                    "gps": {"latitude": 48.9995, "longitude": -122.7404},
                },
            }
        },
    }
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_ACCOUNT_CAMERA_MUNCHY_CONFIG": write_munchy_config(tmp_path, munchy_config),
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    collector = Collector(config)
    collector.init_db()

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def routing_preflight(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True, "left": []}

    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    batch_id = collector.archive_now(account_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(collector, batch_id, config.targets["munchy"])

    assert [item.rel_path for item in request.files] == ["camera/clip.mp4"]
    assert request.job_payload["groups"] == munchy_config["groups"]
    assert request.job_payload["routing"]["routes"] == munchy_config["job"]["routing"]["routes"]
    assert request.storage_hint["structured_routing"] is True
    assert request.storage_hint["groups"] == {
        "camera-video": {"output_mode": "video", "tasks": ["archive_video"]}
    }


def test_jeb_uploads_preflight_left_files_so_munchy_owns_culling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    munchy_config = {
        "schema_version": 1,
        "kind": "munchy.job",
        "job": {
            "output_mode": "video",
            "tasks": ["archive_video"],
            "routing": {
                "routes": [
                    {
                        "id": "main-video",
                        "group": "camera-video",
                        "when": {"path": {"prefix": "camera"}},
                    },
                    {
                        "id": "munchy-left",
                        "action": "leave",
                        "when": {"path": {"filename_glob": "left.mp4"}},
                    },
                ]
            },
        },
        "groups": {
            "camera-video": {
                "output_mode": "video",
                "tasks": ["archive_video"],
            }
        },
    }
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_ACCOUNT_CAMERA_MUNCHY_CONFIG": write_munchy_config(tmp_path, munchy_config),
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    write_stable_file(tmp_path / "landing" / "camera" / "left.mp4")
    collector = Collector(config)
    collector.init_db()

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def routing_preflight(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True, "left": [{"path": "camera/left.mp4"}]}

    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    batch_id = collector.archive_now(account_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(collector, batch_id, config.targets["munchy"])

    assert [item.rel_path for item in request.files] == [
        "camera/clip.mp4",
        "camera/left.mp4",
    ]


def test_archive_now_rediscovers_stale_failed_batch_when_upload_root_changes(
    tmp_path: Path,
) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    initial_config = config_from_env(env_for(tmp_path, accounts="camera"))
    initial_collector = Collector(initial_config)
    initial_collector.init_db()
    initial_batch_id = initial_collector.archive_now(account_id="camera", process=False)
    assert initial_batch_id is not None
    initial_collector.set_attempt_state(initial_batch_id, "failed", "initial config failed")

    updated_account = replace(initial_config.accounts[0], upload_root="camera-v2")
    updated_config = replace(initial_config, accounts=(updated_account,))
    updated_collector = Collector(updated_config)
    updated_collector.init_db()
    updated_batch_id = updated_collector.archive_now(account_id="camera", process=False)

    assert updated_batch_id is not None
    assert updated_batch_id != f"{initial_batch_id}-r2"
    request = munchy_upload_request(
        updated_collector,
        updated_batch_id,
        updated_config.targets["munchy"],
    )
    assert [item.rel_path for item in request.files] == ["camera-v2/clip.mp4"]
    with updated_collector.connect() as conn:
        row = conn.execute(
            "SELECT state FROM batch_attempts WHERE id = ?",
            (initial_batch_id,),
        ).fetchone()
    assert row is not None
    assert row["state"] == "superseded"


def test_jeb_attempt_alerts_use_account_notify_recipients(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "operator",
            "JEB_ACCOUNT_CAMERA_NOTIFY_RECIPIENTS": "operator,collaborator",
            "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    notifier = RecordingNotifier(calls=[])
    collector = Collector(config, notifier=notifier)
    collector.init_db()
    batch_id = collector.archive_now(account_id="camera", process=False)
    assert batch_id is not None
    collector.set_attempt_state(batch_id, "failed", "target failed")

    collector.notify_failed_attempt(batch_id)

    assert notifier.calls[0]["notify"] == {
        "enabled": True,
        "recipients": ["operator", "collaborator"],
    }


def test_jeb_routing_preflight_alerts_use_account_notify_recipients(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "operator",
            "JEB_ACCOUNT_CAMERA_NOTIFY_RECIPIENTS": "operator,collaborator",
            "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
        }
    )
    notifier = RecordingNotifier(calls=[])
    collector = Collector(config, notifier=notifier)
    collector.init_db()
    account = config.accounts[0]
    file = EligibleFile(
        path=account.path / "clip.txt",
        rel=Path("clip.txt"),
        target_path="camera/clip.txt",
        bytes=4,
        mtime=1.0,
        mtime_ns=1,
    )
    collector.store_routing_preflight_failure(
        account=account,
        files=[file],
        failure_kind="routing",
        failure_payload={"ok": False},
        fingerprint_payload={"account_id": "camera", "error": "no route"},
        message="no route",
        file_count=1,
        total_bytes=4,
        unmatched_count=1,
    )

    collector.notify_routing_preflight_failures(account_id="camera")

    assert notifier.calls[0]["notify"] == {
        "enabled": True,
        "recipients": ["operator", "collaborator"],
    }


def test_jeb_status_marks_accounts_with_failed_routing_preflight(
    tmp_path: Path,
) -> None:
    config = config_from_env(env_for(tmp_path, accounts="camera,phone"))
    collector = Collector(config)
    collector.init_db()

    clean_statuses = {
        item["id"]: item for item in collector.status_summary(include_backlog=False)["accounts"]
    }

    assert clean_statuses["camera"]["routing_preflight_failed"] is False
    assert clean_statuses["phone"]["routing_preflight_failed"] is False

    account = config.accounts[0]
    file = EligibleFile(
        path=account.path / "clip.txt",
        rel=Path("clip.txt"),
        target_path="camera/clip.txt",
        bytes=4,
        mtime=1.0,
        mtime_ns=1,
    )
    collector.store_routing_preflight_failure(
        account=account,
        files=[file],
        failure_kind="routing",
        failure_payload={"ok": False},
        fingerprint_payload={"account_id": "camera", "error": "no route"},
        message="no route",
        file_count=1,
        total_bytes=4,
        unmatched_count=1,
    )

    failed_statuses = {
        item["id"]: item for item in collector.status_summary(include_backlog=False)["accounts"]
    }

    assert failed_statuses["camera"]["routing_preflight_failed"] is True
    assert failed_statuses["phone"]["routing_preflight_failed"] is False


def test_jeb_webhook_notifier_routes_named_recipients_to_webhook_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_post_webhook(*, config, payload):
        calls.append((config.url, payload.get("recipient")))

    monkeypatch.setattr(collector_module, "post_webhook", fake_post_webhook)
    notifier = WebhookNotifier(
        NotifySettings(
            enabled=True,
            webhook_urls={
                "operator": "http://operator.test",
                "collaborator": "http://collaborator.test",
            },
        )
    )

    ok = notifier.issue(
        context={
            "id": "batch-1",
            "batch_id": "batch-1",
            "account_id": "camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "camera",
            "collection_timestamp": "2026-07-10T00:00:00Z",
            "state": "failed",
        },
        message="target failed",
        component="target",
        severity="critical",
        notify={"enabled": True, "recipients": ["operator", "collaborator"]},
    )

    assert ok
    assert calls == [
        ("http://operator.test", "operator"),
        ("http://collaborator.test", "collaborator"),
    ]


def test_jeb_webhook_notifier_does_not_fallback_for_missing_named_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_post_webhook(*, config, payload):
        _ = payload
        calls.append(config.url)

    monkeypatch.setattr(collector_module, "post_webhook", fake_post_webhook)
    notifier = WebhookNotifier(
        NotifySettings(
            enabled=True,
            url="http://operator.test",
            webhook_urls={"operator": "http://operator.test"},
        )
    )

    ok = notifier.issue(
        context={
            "id": "batch-1",
            "batch_id": "batch-1",
            "account_id": "camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "camera",
            "collection_timestamp": "2026-07-10T00:00:00Z",
            "state": "failed",
        },
        message="target failed",
        component="target",
        severity="critical",
        notify={"enabled": True, "recipients": ["collaborator"]},
    )

    assert not ok
    assert calls == []


def test_jeb_routing_preflight_collects_primary_facts_after_sidecar_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing = {
        "extra_exiftool_tags": ["VideoAvgBitrate"],
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}M01.XML",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {"source": "exiftool", "tags": ["Make"]},
            }
        ],
        "routes": [
            {
                "id": "sidecar-and-bitrate-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {
                            "fact": "sidecars.camera_xml.facts.exif.make",
                            "equals": "example imaging",
                        },
                        {
                            "fact": "exiftool.tags.video_avg_bitrate",
                            "equals": "200 Mbps",
                        },
                    ]
                },
            }
        ],
    }
    config = config_from_env(env_for(tmp_path, accounts="camera"))
    config = replace(
        config,
        munchy_job_defaults={
            **dict(config.munchy_job_defaults),
            "routing": routing,
        },
    )
    collector = Collector(config)
    collector.init_db()
    account = config.accounts[0]
    video = account.path / "C0001.MP4"
    sidecar = account.path / "C0001M01.XML"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    sidecar.write_text("<metadata />", encoding="utf-8")
    files = [
        EligibleFile(
            path=video,
            rel=Path("C0001.MP4"),
            target_path="camera/C0001.MP4",
            bytes=video.stat().st_size,
            mtime=1.0,
            mtime_ns=1,
        ),
        EligibleFile(
            path=sidecar,
            rel=Path("C0001M01.XML"),
            target_path="camera/C0001M01.XML",
            bytes=sidecar.stat().st_size,
            mtime=1.0,
            mtime_ns=2,
        ),
    ]
    exiftool_calls: list[tuple[Path, tuple[str, ...]]] = []
    preflight_files = []

    def fail_ffprobe(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for exiftool-only routing: {path}")

    def fake_exiftool(path: Path, *, tags=()):  # type: ignore[no-untyped-def]
        exiftool_calls.append((path, tuple(tags)))
        if path == sidecar:
            return {"EXIF:Make": "Example Imaging"}
        if path == video:
            assert "VideoAvgBitrate" in tuple(tags)
            return {"QuickTime:VideoAvgBitrate": "200 Mbps"}
        raise AssertionError(f"unexpected exiftool path: {path}")

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def routing_preflight(self, **kwargs: object) -> dict[str, object]:
            preflight_files.extend(kwargs["files"])  # type: ignore[arg-type]
            return {"ok": True, "left": []}

    monkeypatch.setattr(collector_module, "ffprobe_for_routing_preflight", fail_ffprobe)
    monkeypatch.setattr(collector_module, "exiftool_for_routing_preflight", fake_exiftool)
    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    routed = collector.preflight_account_routes(account, files)

    assert routed == files
    assert [path for path, _tags in exiftool_calls] == [sidecar, video]
    assert exiftool_calls[0] == (sidecar, ("Make",))
    primary = next(item for item in preflight_files if item.rel_path == "camera/C0001.MP4")
    assert primary.routing_facts["exiftool"]["tags"]["video_avg_bitrate"] == "200 Mbps"
