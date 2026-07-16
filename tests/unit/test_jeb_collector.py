from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from jeb.sources import SourceRegistryError

TEST_POLICY = {
    "workflow_mode": "collection_archive",
    "output_mode": "video",
    "tasks": ["archive_video"],
    "collection_archive": {"destination": "riverhog"},
}


def env_for(tmp_path: Path, *, sources: str = "camera,phone") -> dict[str, str]:
    return {
        "TEST_SOURCE_IDS": sources,
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://runner.test",
    }


def collector_from_env(
    env: dict[str, str],
    *,
    target_runners=None,
    notifier=None,
    options: dict[str, dict[str, object]] | None = None,
    policies: dict[str, dict[str, object]] | None = None,
) -> Collector:
    source_ids = env.get("TEST_SOURCE_IDS", "").split(",")
    runtime_env = {key: value for key, value in env.items() if not key.startswith("TEST_")}
    collector = Collector(
        config_from_env(runtime_env),
        target_runners=target_runners,
        notifier=notifier,
    )
    collector.init_db()
    for source_id in source_ids:
        if not source_id:
            continue
        source_options = dict((options or {}).get(source_id, {}))
        collector.add_source(
            source_id,
            adapters=("ftp", "tus"),
            policy=(policies or {}).get(source_id, TEST_POLICY),
            credential=f"{source_id}-password",
            stable_seconds=0,
            include_extensions=(".txt", ".mp4", ".xml"),
            **source_options,
        )
    return collector


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


def test_runtime_config_and_source_registry_have_distinct_authority(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path))

    assert collector.config.collector.batch_dir == tmp_path / "landing" / ".jeb-batches"
    sources = collector.source_registry.list()
    assert [(source.id, source.collection_slug) for source in sources] == [
        ("camera", "camera"),
        ("phone", "phone"),
    ]
    assert [source.policy for source in sources] == [TEST_POLICY, TEST_POLICY]


def test_source_policy_revisions_and_ftp_projection_are_registry_owned(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path, sources="camera"))
    source = collector.source_registry.get("camera")

    assert source.policy_revision == 1
    projection = collector.config.ingress.ftp_projection.read_text(encoding="utf-8")
    assert projection.startswith("camera:$argon2id$")

    updated = collector.update_source(
        "camera",
        {
            "cadence": "manual",
            "notify": {"enabled": True, "recipients": ["operator"]},
            "policy": {**TEST_POLICY, "tasks": ["archive_video", "qcut_video"]},
        },
    )

    assert updated.cadence == "manual"
    assert updated.notify == {"enabled": True, "recipients": ["operator"]}
    assert updated.policy_revision == 2
    with collector.connect() as conn:
        revisions = conn.execute(
            "SELECT revision FROM source_policy_revisions WHERE source_id = ? ORDER BY revision",
            ("camera",),
        ).fetchall()
    assert [row["revision"] for row in revisions] == [1, 2]


def test_source_registry_lists_compact_filtered_pages_in_sql(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path))
    collector.source_registry.set_enabled("phone", False)
    collector.source_registry.update(
        "phone",
        {
            "adapters": ["tus"],
            "collection_slug": "mobile",
            "target": "review",
            "cadence": "manual",
        },
    )

    page = collector.source_registry.list_page(
        page=1,
        per_page=1,
        sort="id",
        order="desc",
    )

    assert page["total"] == 2
    assert page["pages"] == 2
    assert page["per_page"] == 1
    assert [source["id"] for source in page["sources"]] == ["phone"]
    assert "policy" not in page["sources"][0]

    filtered = collector.source_registry.list_page(
        query="MOBILE",
        enabled=False,
        adapter="tus",
        target="review",
        all_items=True,
    )

    assert filtered["page"] == 1
    assert filtered["per_page"] == 1
    assert filtered["pages"] == 1
    assert filtered["filters"] == {
        "enabled": False,
        "adapter": "tus",
        "target": "review",
    }
    assert [source["id"] for source in filtered["sources"]] == ["phone"]

    with pytest.raises(SourceRegistryError, match="between 1 and 100"):
        collector.source_registry.list_page(per_page=101)


def test_jeb_schema_indexes_operator_status_and_list_paths(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path))

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
        "idx_jeb_batches_source",
        "idx_jeb_batches_source_period",
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
    env = env_for(tmp_path, sources="camera")
    write_stable_file(tmp_path / "landing" / "camera" / "a.txt", b"aaa")
    write_stable_file(tmp_path / "landing" / "camera" / "b.txt", b"bbbbb")
    collector = collector_from_env(env)
    batch_id = collector.archive_now(source_id="camera", process=False)
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

    assert collector.list_attempts(terminal="all", source="camera")["total"] == 1

    with collector.connect() as conn:
        conn.execute(
            "DELETE FROM files WHERE batch_id = ? AND target_path = ?",
            (batch_id, "camera/a.txt"),
        )

    page = collector.list_attempts(terminal="all", sort="bytes")
    assert page["attempts"][0]["source_id"] == "camera"
    assert page["attempts"][0]["file_count"] == 0
    assert page["attempts"][0]["total_bytes"] == 0
    assert collector.list_attempts(terminal="all", source="camera")["total"] == 1


def test_env_config_loads_named_notify_webhook_map(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, sources="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "operator,collaborator",
            "RIVERHOG_COLLECTION_WEBHOOKS": '{"operator":"http://operator.test","collaborator":"http://collaborator.test"}',
        }
    )

    assert config.notify.webhook_urls == {
        "operator": "http://operator.test",
        "collaborator": "http://collaborator.test",
    }


def test_source_registry_requires_safe_source_slugs(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path, sources=""))

    with pytest.raises(SourceRegistryError, match="safe slug"):
        collector.add_source(
            "phone/raw",
            adapters=("tus",),
            policy=TEST_POLICY,
        )


def test_cleanup_after_success_requires_safe_munchy_target(tmp_path: Path) -> None:
    env = {**env_for(tmp_path, sources=""), "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE": "false"}
    collector = collector_from_env(env)

    with pytest.raises(SourceRegistryError, match="safe-delete"):
        collector.add_source(
            "camera",
            adapters=("ftp",),
            policy=TEST_POLICY,
            cleanup="after_target_success",
        )


def test_source_credential_rotation_updates_all_enabled_adapters(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path, sources="camera"))

    collector.source_registry.authenticate("camera", "camera-password", adapter="ftp")
    collector.source_registry.authenticate("camera", "camera-password", adapter="tus")
    _source, credential = collector.source_registry.rotate_credential(
        "camera",
        credential="replacement-password",
    )

    assert credential is None
    with pytest.raises(SourceRegistryError, match="invalid Jeb ingress credentials"):
        collector.source_registry.authenticate("camera", "camera-password", adapter="tus")
    collector.source_registry.authenticate("camera", "replacement-password", adapter="ftp")
    assert "camera:$argon2id$" in collector.config.ingress.ftp_projection.read_text(
        encoding="utf-8"
    )


def test_source_purge_is_plan_bound_guarded_and_idempotent(tmp_path: Path) -> None:
    collector = collector_from_env(env_for(tmp_path, sources="camera"))
    landing_file = tmp_path / "landing" / "camera" / "clip.txt"
    write_stable_file(landing_file, b"first")
    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None

    blocked = collector.source_removal_plan("camera", purge=False)
    assert blocked["status"] == "blocked"
    assert blocked["challenge"] is None

    stale = collector.source_removal_plan("camera", purge=True)
    assert stale["status"] == "ready"
    assert stale["warning"] == collector_module.SOURCE_PURGE_WARNING
    landing_file.write_bytes(b"changed after planning")
    with pytest.raises(collector_module.UnrecoverableJebError, match="plan changed"):
        collector.remove_source("camera", challenge=stale["challenge"])

    plan = collector.source_removal_plan("camera", purge=True)
    result = collector.remove_source("camera", challenge=plan["challenge"])

    assert result == {
        "status": "removed",
        "source": "camera",
        "purged": True,
        "files": 1,
        "bytes": len(b"changed after planning"),
    }
    assert not landing_file.exists()
    with pytest.raises(SourceRegistryError, match="source not found"):
        collector.source_registry.get("camera")
    assert collector.remove_source("camera", challenge=plan["challenge"]) == result


def test_scheduler_batches_each_source_independently(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")
    runner = CompleteRunner()
    collector = collector_from_env(
        env_for(tmp_path),
        target_runners={"munchy": runner},
    )

    collector.run_once()
    assert len(active_attempt_ids(collector)) == 2
    collector.run_once()

    assert runner.calls == 2
    with collector.connect() as conn:
        batches = conn.execute(
            "SELECT source_id, collection_slug FROM batches ORDER BY source_id"
        ).fetchall()
    assert sorted({(row["source_id"], row["collection_slug"]) for row in batches}) == [
        ("camera", "camera"),
        ("phone", "phone"),
    ]


def test_manual_cadence_only_runs_with_archive_now(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        options={"camera": {"cadence": "manual"}},
    )

    collector.run_once()
    assert active_attempt_ids(collector) == []

    assert collector.archive_now(source_id="camera", process=False) is not None
    assert len(active_attempt_ids(collector)) == 1


def test_archive_plan_reports_batch_without_creating_it(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt", b"camera")
    collector = collector_from_env(env_for(tmp_path, sources="camera"))

    plan = collector.archive_plan(source_id="camera")

    assert plan["status"] == "would_process"
    assert plan["dry_run"] is True
    assert plan["source"] == "camera"
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
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    policy = {**munchy_config["job"], "groups": munchy_config["groups"]}
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        policies={"camera": policy},
    )

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

    plan = collector.archive_plan(source_id="camera")

    assert plan["status"] == "routing_preflight_failed"
    assert plan["routing_preflight"]["unmatched_count"] == 1
    assert active_attempt_ids(collector) == []
    assert collector.routing_preflight_failures(state="failed") == []


def test_monthly_cadence_uses_first_scheduled_run_after_month_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = collector_from_env(
        env_for(tmp_path, sources="phone"),
        options={"phone": {"cadence": "monthly", "weekday": 0, "hour": 3, "minute": 0}},
    )
    collection = collector.source_registry.get("phone")

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert collector.source_period(collection) == datetime(2026, 7, 6, 3, tzinfo=UTC)

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 7, 2, 12, tzinfo=UTC))
    assert collector.source_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)


def test_seasonal_cadence_uses_first_scheduled_run_after_custom_season_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = collector_from_env(
        env_for(tmp_path, sources="phone"),
        options={"phone": {"cadence": "seasonal", "weekday": 0, "hour": 3, "minute": 0}},
    )
    collection = collector.source_registry.get("phone")

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert collector.source_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)

    monkeypatch.setattr("jeb.collector.current_time", lambda: datetime(2026, 6, 1, 2, tzinfo=UTC))
    assert collector.source_period(collection) == datetime(2026, 3, 2, 3, tzinfo=UTC)


def test_munchy_payload_uses_source_group_paths_without_routing(tmp_path: Path) -> None:
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        options={
            "camera": {
                "notify": {"enabled": True, "recipients": ["operator", "collaborator"]}
            }
        },
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")

    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(collector, batch_id, collector.config.targets["munchy"])

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
    assert "routing" not in request.job_payload


def test_munchy_payload_uses_per_source_job_defaults(
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
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    policy = {**munchy_config["job"], "groups": munchy_config["groups"]}
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        policies={"camera": policy},
    )

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def routing_preflight(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True, "left": []}

    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(
        collector,
        batch_id,
        collector.config.targets["munchy"],
    )

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
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    write_stable_file(tmp_path / "landing" / "camera" / "left.mp4")
    policy = {**munchy_config["job"], "groups": munchy_config["groups"]}
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        policies={"camera": policy},
    )

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def routing_preflight(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True, "left": [{"path": "camera/left.mp4"}]}

    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(
        collector,
        batch_id,
        collector.config.targets["munchy"],
    )

    assert [item.rel_path for item in request.files] == [
        "camera/clip.mp4",
        "camera/left.mp4",
    ]


def test_jeb_attempt_alerts_use_source_notify_recipients(tmp_path: Path) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    notifier = RecordingNotifier(calls=[])
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        notifier=notifier,
        options={
            "camera": {
                "notify": {"enabled": True, "recipients": ["operator", "collaborator"]}
            }
        },
    )
    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    collector.set_attempt_state(batch_id, "failed", "target failed")

    collector.notify_failed_attempt(batch_id)

    assert notifier.calls[0]["notify"] == {
        "enabled": True,
        "recipients": ["operator", "collaborator"],
    }


def test_jeb_routing_preflight_alerts_use_source_notify_recipients(tmp_path: Path) -> None:
    notifier = RecordingNotifier(calls=[])
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        notifier=notifier,
        options={
            "camera": {
                "notify": {"enabled": True, "recipients": ["operator", "collaborator"]}
            }
        },
    )
    source = collector.source_registry.get("camera")
    file = EligibleFile(
        path=source.path / "clip.txt",
        rel=Path("clip.txt"),
        target_path="camera/clip.txt",
        bytes=4,
        mtime=1.0,
        mtime_ns=1,
    )
    collector.store_routing_preflight_failure(
        source=source,
        files=[file],
        failure_kind="routing",
        failure_payload={"ok": False},
        fingerprint_payload={"source_id": "camera", "error": "no route"},
        message="no route",
        file_count=1,
        total_bytes=4,
        unmatched_count=1,
    )

    collector.notify_routing_preflight_failures(source_id="camera")

    assert notifier.calls[0]["notify"] == {
        "enabled": True,
        "recipients": ["operator", "collaborator"],
    }


def test_jeb_status_marks_sources_with_failed_routing_preflight(
    tmp_path: Path,
) -> None:
    collector = collector_from_env(env_for(tmp_path, sources="camera,phone"))

    clean_statuses = {
        item["id"]: item for item in collector.status_summary(include_backlog=False)["sources"]
    }

    assert clean_statuses["camera"]["routing_preflight_failed"] is False
    assert clean_statuses["phone"]["routing_preflight_failed"] is False

    source = collector.source_registry.get("camera")
    file = EligibleFile(
        path=source.path / "clip.txt",
        rel=Path("clip.txt"),
        target_path="camera/clip.txt",
        bytes=4,
        mtime=1.0,
        mtime_ns=1,
    )
    collector.store_routing_preflight_failure(
        source=source,
        files=[file],
        failure_kind="routing",
        failure_payload={"ok": False},
        fingerprint_payload={"source_id": "camera", "error": "no route"},
        message="no route",
        file_count=1,
        total_bytes=4,
        unmatched_count=1,
    )

    failed_statuses = {
        item["id"]: item for item in collector.status_summary(include_backlog=False)["sources"]
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
            "source_id": "camera",
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
            "source_id": "camera",
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
    collector = collector_from_env(
        env_for(tmp_path, sources="camera"),
        policies={"camera": {**TEST_POLICY, "routing": routing}},
    )
    source = collector.source_registry.get("camera")
    video = source.path / "C0001.MP4"
    sidecar = source.path / "C0001M01.XML"
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

    routed = collector.preflight_source_routes(source, files)

    assert routed == files
    assert [path for path, _tags in exiftool_calls] == [sidecar, video]
    assert exiftool_calls[0] == (sidecar, ("Make",))
    primary = next(item for item in preflight_files if item.rel_path == "camera/C0001.MP4")
    assert primary.routing_facts["exiftool"]["tags"]["video_avg_bitrate"] == "200 Mbps"
