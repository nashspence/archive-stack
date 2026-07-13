from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
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


def write_stable_file(path: Path, content: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def active_batch_ids(collector: Collector) -> list[str]:
    return [str(row["id"]) for row in collector.active_batches()]


@dataclass
class CompleteRunner:
    calls: int = 0

    def advance(self, collector: Collector, batch_id: str) -> None:
        self.calls += 1
        collector.set_batch_state(batch_id, "target_complete")


@dataclass
class RecordingNotifier:
    calls: list[dict[str, object]]

    def critical_batch_issue(
        self,
        *,
        batch,
        message,
        component,
        notify=None,
    ) -> bool:
        self.calls.append(
            {
                "batch": dict(batch),
                "message": message,
                "component": component,
                "notify": dict(notify or {}),
            }
        )
        return True

    def enrollment_issue(
        self,
        *,
        batch,
        message,
        component,
        notify=None,
    ) -> bool:
        self.calls.append(
            {
                "batch": dict(batch),
                "message": message,
                "component": component,
                "notify": dict(notify or {}),
            }
        )
        return True


def test_parse_helpers_support_human_units() -> None:
    assert parse_duration("10m") == 600
    assert parse_duration("2h") == 7200
    assert parse_size("2GiB") == 2 * 1024**3


def test_env_config_creates_one_account_collection_per_account(tmp_path: Path) -> None:
    config = config_from_env(env_for(tmp_path))

    assert [source.id for source in config.sources] == ["camera", "phone"]
    assert [str(source.path) for source in config.sources] == [
        str(tmp_path / "landing" / "camera"),
        str(tmp_path / "landing" / "phone"),
    ]
    assert config.collector.batch_dir == tmp_path / "landing" / ".jeb-batches"
    collection_keys = [
        (collection.id, collection.collection_slug, collection.source_ids)
        for collection in config.collections
    ]
    assert collection_keys == [
        ("camera", "camera", ("camera",)),
        ("phone", "phone", ("phone",)),
    ]
    assert config.munchy_job_defaults["tasks"] == ["archive_video"]


def test_env_config_supports_per_account_upload_prefix_and_munchy_job(
    tmp_path: Path,
) -> None:
    job_config = {
        "groups": {
            "camera-video": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
                "metadata_projection": {
                    "gps": {"latitude": 48.9995, "longitude": -122.7404},
                },
            }
        },
        "profile_routing": {
            "routes": [
                {
                    "id": "main-video",
                    "group": "camera-video",
                    "when": {"path": {"prefix": "camera-video"}},
                }
            ]
        },
    }
    env = {
        **env_for(tmp_path, accounts="camera"),
        "JEB_ACCOUNT_CAMERA_UPLOAD_PREFIX": "camera-video",
        "JEB_ACCOUNT_CAMERA_MUNCHY_JOB_JSON": json.dumps(job_config, separators=(",", ":")),
    }

    config = config_from_env(env)

    assert config.sources[0].upload_prefix == "camera-video"
    assert dict(config.collections[0].munchy_job_defaults) == job_config


def test_env_config_supports_per_account_cadence(tmp_path: Path) -> None:
    env = {
        **env_for(tmp_path, accounts="front-door,phone"),
        "JEB_CADENCE": "monthly",
        "JEB_ACCOUNT_FRONT_DOOR_CADENCE": "seasonal",
        "JEB_ACCOUNT_PHONE_CADENCE": "manual",
    }

    config = config_from_env(env)

    assert [collection.cadence for collection in config.collections] == [
        "seasonal",
        "manual",
    ]


def test_env_config_supports_per_account_munchy_notify(tmp_path: Path) -> None:
    env = {
        **env_for(tmp_path, accounts="front-door,phone"),
        "JEB_NOTIFY_ENABLED": "true",
        "JEB_NOTIFY_RECIPIENTS": "nash",
        "JEB_ACCOUNT_FRONT_DOOR_NOTIFY_RECIPIENTS": "nash,katie",
        "JEB_ACCOUNT_PHONE_NOTIFY_ENABLED": "false",
        "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
    }

    config = config_from_env(env)

    assert [collection.notify for collection in config.collections] == [
        {"enabled": True, "recipients": ["nash", "katie"]},
        {"enabled": False, "recipients": ["nash"]},
    ]
    assert config.notify.webhook_urls == {}


def test_env_config_loads_named_notify_webhook_map(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "nash,katie",
            "RIVERHOG_NOTIFY_WEBHOOKS": '{"nash":"http://nash.test","katie":"http://katie.test"}',
        }
    )

    assert config.notify.webhook_urls == {
        "nash": "http://nash.test",
        "katie": "http://katie.test",
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
    assert len(active_batch_ids(collector)) == 2
    collector.run_once()

    assert runner.calls == 2
    with collector.connect() as conn:
        batches = conn.execute(
            "SELECT collection_id, collection_slug FROM batches ORDER BY collection_id"
        ).fetchall()
    assert sorted({(row["collection_id"], row["collection_slug"]) for row in batches}) == [
        ("camera", "camera"),
        ("phone", "phone"),
    ]


def test_manual_cadence_only_runs_with_archive_now(tmp_path: Path) -> None:
    config = config_from_env({**env_for(tmp_path, accounts="camera"), "JEB_CADENCE": "manual"})
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    collector = Collector(config)

    collector.run_once()
    assert active_batch_ids(collector) == []

    assert collector.archive_now(source_id="camera", process=False) is not None
    assert len(active_batch_ids(collector)) == 1


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
    [collection] = config.collections

    monkeypatch.setattr("jeb.collector.now", lambda: datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert collector.collection_period(collection) == datetime(2026, 7, 6, 3, tzinfo=UTC)

    monkeypatch.setattr("jeb.collector.now", lambda: datetime(2026, 7, 2, 12, tzinfo=UTC))
    assert collector.collection_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)


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
    [collection] = config.collections

    monkeypatch.setattr("jeb.collector.now", lambda: datetime(2026, 7, 8, 12, tzinfo=UTC))
    assert collector.collection_period(collection) == datetime(2026, 6, 1, 3, tzinfo=UTC)

    monkeypatch.setattr("jeb.collector.now", lambda: datetime(2026, 6, 1, 2, tzinfo=UTC))
    assert collector.collection_period(collection) == datetime(2026, 3, 2, 3, tzinfo=UTC)


def test_munchy_payload_uses_account_group_paths_without_routing(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "nash",
            "JEB_ACCOUNT_CAMERA_NOTIFY_RECIPIENTS": "nash,katie",
            "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    collector = Collector(config)
    collector.init_db()

    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(collector, batch_id, config.targets["munchy"])

    assert [item.rel_path for item in request.files] == ["camera/clip.txt"]
    assert request.storage_hint == {
        "workflow_mode": "collection_archive",
        "collection_archive_destination": "riverhog",
        "archive_mode": "av1_nvenc",
        "tasks": ["archive_video"],
        "structured_routing": False,
        "groups": {},
    }
    assert request.job_payload["collection_slug"] == "camera"
    assert request.job_payload["tasks"] == ["archive_video"]
    assert request.job_payload["groups"] == {}
    assert request.job_payload["notify"] == {"enabled": True, "recipients": ["nash", "katie"]}
    assert "profile_routing" not in request.job_payload


def test_munchy_payload_uses_per_account_job_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_config = {
        "archive_mode": "av1_nvenc",
        "tasks": ["archive_video"],
        "groups": {
            "camera-video": {
                "archive_mode": "av1_nvenc",
                "tasks": ["archive_video"],
                "metadata_projection": {
                    "gps": {"latitude": 48.9995, "longitude": -122.7404},
                },
            }
        },
        "profile_routing": {
            "routes": [
                {
                    "id": "main-video",
                    "group": "camera-video",
                    "when": {"path": {"prefix": "camera-video"}},
                }
            ]
        },
    }
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_ACCOUNT_CAMERA_UPLOAD_PREFIX": "camera-video",
            "JEB_ACCOUNT_CAMERA_MUNCHY_JOB_JSON": json.dumps(job_config, separators=(",", ":")),
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    collector = Collector(config)
    collector.init_db()

    class FakeMunchyRunnerClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def profile_routing_preflight(self, **_kwargs: object) -> dict[str, object]:
            return {"ok": True, "left": []}

    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    request = munchy_upload_request(collector, batch_id, config.targets["munchy"])

    assert [item.rel_path for item in request.files] == ["camera-video/clip.mp4"]
    assert request.job_payload["groups"] == job_config["groups"]
    assert request.job_payload["profile_routing"] == job_config["profile_routing"]
    assert request.storage_hint["structured_routing"] is True
    assert request.storage_hint["groups"] == {
        "camera-video": {"archive_mode": "av1_nvenc", "tasks": ["archive_video"]}
    }


def test_archive_now_rediscovers_stale_failed_batch_when_upload_prefix_changes(
    tmp_path: Path,
) -> None:
    write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    old_config = config_from_env(env_for(tmp_path, accounts="camera"))
    old_collector = Collector(old_config)
    old_collector.init_db()
    old_batch_id = old_collector.archive_now(source_id="camera", process=False)
    assert old_batch_id is not None
    old_collector.set_batch_state(old_batch_id, "failed", "old config failed")

    new_config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_ACCOUNT_CAMERA_UPLOAD_PREFIX": "camera-video",
        }
    )
    new_collector = Collector(new_config)
    new_collector.init_db()
    new_batch_id = new_collector.archive_now(source_id="camera", process=False)

    assert new_batch_id is not None
    assert new_batch_id != f"{old_batch_id}-r2"
    request = munchy_upload_request(new_collector, new_batch_id, new_config.targets["munchy"])
    assert [item.rel_path for item in request.files] == ["camera-video/clip.mp4"]
    with new_collector.connect() as conn:
        row = conn.execute(
            "SELECT state FROM batch_attempts WHERE id = ?",
            (old_batch_id,),
        ).fetchone()
    assert row is not None
    assert row["state"] == "superseded"


def test_jeb_batch_alerts_use_account_notify_recipients(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "nash",
            "JEB_ACCOUNT_CAMERA_NOTIFY_RECIPIENTS": "nash,katie",
            "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
        }
    )
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt")
    notifier = RecordingNotifier(calls=[])
    collector = Collector(config, notifier=notifier)
    collector.init_db()
    batch_id = collector.archive_now(source_id="camera", process=False)
    assert batch_id is not None
    collector.set_batch_state(batch_id, "failed", "target failed")

    collector.notify_failed_batch(batch_id)

    assert notifier.calls[0]["notify"] == {"enabled": True, "recipients": ["nash", "katie"]}


def test_jeb_routing_preflight_alerts_use_account_notify_recipients(tmp_path: Path) -> None:
    config = config_from_env(
        {
            **env_for(tmp_path, accounts="camera"),
            "JEB_NOTIFY_ENABLED": "true",
            "JEB_NOTIFY_RECIPIENTS": "nash",
            "JEB_ACCOUNT_CAMERA_NOTIFY_RECIPIENTS": "nash,katie",
            "RIVERHOG_OPERATOR_WEBHOOK_URL": "http://webhook.test",
        }
    )
    notifier = RecordingNotifier(calls=[])
    collector = Collector(config, notifier=notifier)
    collector.init_db()
    source = config.sources[0]
    collection = config.collections[0]
    file = EligibleFile(
        path=source.path / "clip.txt",
        rel=Path("clip.txt"),
        target_path="camera/clip.txt",
        bytes=4,
        mtime=1.0,
        mtime_ns=1,
    )
    collector.store_routing_preflight_failure(
        collection=collection,
        source=source,
        files=[file],
        failure_kind="profile_routing",
        failure_payload={"ok": False},
        fingerprint_payload={"source_id": "camera", "error": "no route"},
        message="no route",
        file_count=1,
        total_bytes=4,
        unmatched_count=1,
    )

    collector.notify_routing_preflight_failures(source_id="camera")

    assert notifier.calls[0]["notify"] == {"enabled": True, "recipients": ["nash", "katie"]}


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
                "nash": "http://nash.test",
                "katie": "http://katie.test",
            },
        )
    )

    ok = notifier.critical_batch_issue(
        batch={
            "id": "batch-1",
            "source_id": "camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "camera",
            "collection_timestamp": "2026-07-10T00:00:00Z",
            "state": "failed",
        },
        message="target failed",
        component="target",
        notify={"enabled": True, "recipients": ["nash", "katie"]},
    )

    assert ok
    assert calls == [
        ("http://nash.test", "nash"),
        ("http://katie.test", "katie"),
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
            webhook_urls={"nash": "http://nash.test"},
        )
    )

    ok = notifier.critical_batch_issue(
        batch={
            "id": "batch-1",
            "source_id": "camera",
            "target_name": "munchy",
            "target_type": "munchy",
            "collection_slug": "camera",
            "collection_timestamp": "2026-07-10T00:00:00Z",
            "state": "failed",
        },
        message="target failed",
        component="target",
        notify={"enabled": True, "recipients": ["katie"]},
    )

    assert not ok
    assert calls == []


def test_jeb_routing_preflight_collects_primary_facts_after_sidecar_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_routing = {
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
            "profile_routing": profile_routing,
        },
    )
    collector = Collector(config)
    collector.init_db()
    source = config.sources[0]
    collection = config.collections[0]
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

        def profile_routing_preflight(self, **kwargs: object) -> dict[str, object]:
            preflight_files.extend(kwargs["files"])  # type: ignore[arg-type]
            return {"ok": True, "left": []}

    monkeypatch.setattr(collector_module, "ffprobe_for_routing_preflight", fail_ffprobe)
    monkeypatch.setattr(collector_module, "exiftool_for_routing_preflight", fake_exiftool)
    monkeypatch.setattr(collector_module, "MunchyRunnerClient", FakeMunchyRunnerClient)

    routed = collector.preflight_source_routes(collection, source, files)

    assert routed == files
    assert [path for path, _tags in exiftool_calls] == [sidecar, video]
    assert exiftool_calls[0] == (sidecar, ("Make",))
    primary = next(item for item in preflight_files if item.rel_path == "camera/C0001.MP4")
    assert primary.routing_facts["exiftool"]["tags"]["video_avg_bitrate"] == "200 Mbps"
