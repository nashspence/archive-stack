from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from jeb.collector import (
    Collector,
    JebConfig,
    TransientJebError,
    UnrecoverableJebError,
    config_from_mapping,
    munchy_upload_request,
    parse_duration,
    parse_size,
)


def _base_config(
    tmp_path: Path,
    *,
    cleanup: str = "never",
    wait_for_safe_delete: bool = True,
    source_ids: list[str] | None = None,
) -> JebConfig:
    landing = tmp_path / "landing"
    landing.mkdir(exist_ok=True)
    sources = source_ids or ["camera"]
    return config_from_mapping(
        {
            "collector": {
                "interval": "5m",
                "state_db": str(tmp_path / "state.sqlite3"),
                "batch_dir": str(tmp_path / "batches"),
            },
            "targets": {
                "runner": {
                    "type": "munchy",
                    "url": "http://runner.test",
                    "upload_workers": 2,
                    "upload_chunk_mib": 16,
                    "wait_for_safe_delete": wait_for_safe_delete,
                }
            },
            "profiles": {
                "security": {
                    "schema_version": 1,
                    "target": "munchy-av1-nvenc",
                    "archive": {
                        "container": "webm",
                        "quality": 38,
                        "max_height": 720,
                        "scale_flags": "lanczos",
                        "preset": "p7",
                        "tune": "uhq",
                    },
                }
            },
            "profile_groups": {
                "video": {
                    "profile": "security",
                    "archive_mode": "av1_nvenc",
                    "gpu_tasks": ["archive_video"],
                },
                "passthrough": {
                    "archive_mode": "passthrough",
                },
            },
            "munchy_job_defaults": {
                "workflow_mode": "archive",
                "riverhog": {"enabled": True},
                "notify": {"enabled": True, "recipients": ["operator"]},
                "profile_routing": {
                    "routes": [
                        {
                            "id": "camera-video",
                            "group": "video",
                            "path_prefix": "camera",
                            "suffixes": [".mp4", ".mov", ".mkv", ".webm"],
                        },
                        {
                            "id": "phone-video",
                            "group": "video",
                            "path_prefix": "phone",
                            "suffixes": [".mp4", ".mov", ".mkv", ".webm"],
                        },
                        {
                            "id": "passthrough-artifacts",
                            "group": "passthrough",
                            "suffixes": [".xml", ".json", ".txt"],
                        },
                    ]
                },
            },
            "sources": [
                {
                    "id": source_id,
                    "path": str(landing / source_id),
                    "upload_prefix": source_id,
                    "stable_age": "0s",
                    "include_extensions": [".mp4", ".mov", ".mkv", ".webm", ".xml", ".json"],
                }
                for source_id in sources
            ],
            "collections": [
                {
                    "id": "weekly-device-artifacts",
                    "collection_slug": "weekly-device-artifacts",
                    "target": "runner",
                    "threshold": "1B",
                    "cleanup": cleanup,
                    "schedule": "weekly",
                    "weekday": "monday",
                    "hour": 0,
                    "minute": 0,
                    "sources": sources,
                }
            ],
        }
    )


class CompleteRunner:
    def advance(self, collector: Collector, batch_id: str) -> None:
        collector.set_batch_state(batch_id, "target_complete")


@dataclass
class FlakyRunner:
    calls: int = 0

    def advance(self, collector: Collector, batch_id: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise TransientJebError("connection reset by peer")
        collector.set_batch_state(batch_id, "target_complete")


@dataclass
class BrokenRunner:
    message: str = "bad profile mapping"

    def advance(self, collector: Collector, batch_id: str) -> None:
        raise UnrecoverableJebError(self.message)


@dataclass
class RecordingNotifier:
    messages: list[str]

    def critical_batch_issue(
        self,
        *,
        batch: dict[str, object],
        message: str,
        component: str,
    ) -> bool:
        self.messages.append(f"{batch['id']}:{component}:{message}")
        return True


def _write_stable_file(path: Path, content: bytes = b"video") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def _single_batch_id(collector: Collector) -> str:
    batches = collector.active_batch_ids()
    assert len(batches) == 1
    return batches[0]


def test_parse_helpers_support_human_units() -> None:
    assert parse_size("25GB") == 25_000_000_000
    assert parse_size("1GiB") == 1024**3
    assert parse_duration("10m") == 600
    assert parse_duration("24h") == 86_400


def test_weekly_collection_batches_multiple_sources_once(tmp_path: Path) -> None:
    config = _base_config(tmp_path, source_ids=["camera", "phone"])
    _write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    _write_stable_file(tmp_path / "landing" / "phone" / "IMG_0001.MOV")
    collector = Collector(config, target_runners={"munchy": CompleteRunner()})

    collector.run_once()
    batch_id = _single_batch_id(collector)
    target_paths = [str(row["target_path"]) for row in collector.batch_files(batch_id)]

    assert target_paths == ["camera/clip.mp4", "phone/IMG_0001.MOV"]

    collector.run_once()
    assert collector.load_batch(batch_id)["state"] == "target_succeeded"

    collector.run_once()
    assert collector.active_batch_ids() == []


def test_restartable_batch_stages_hashes_and_cleans_after_success(tmp_path: Path) -> None:
    config = _base_config(tmp_path, cleanup="after_target_success")
    source_file = tmp_path / "landing" / "camera" / "clip.mp4"
    _write_stable_file(source_file)
    collector = Collector(config, target_runners={"munchy": CompleteRunner()})

    collector.run_once()
    batch_id = _single_batch_id(collector)
    assert source_file.exists()
    assert not (tmp_path / "batches" / batch_id).exists()

    collector.run_once()

    assert not source_file.exists()
    assert not (tmp_path / "batches" / batch_id).exists()
    assert collector.load_batch(batch_id)["state"] == "cleanup_done"


def test_transient_target_errors_retry_without_notification(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    _write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    notifier = RecordingNotifier([])
    runner = FlakyRunner()
    collector = Collector(config, target_runners={"munchy": runner}, notifier=notifier)

    collector.run_once()
    batch_id = _single_batch_id(collector)
    collector.run_once()

    assert runner.calls == 1
    assert collector.load_batch(batch_id)["state"] == "hashed"
    assert collector.load_batch(batch_id)["last_error"] == "connection reset by peer"
    assert notifier.messages == []

    collector.run_once()

    assert runner.calls == 2
    assert collector.load_batch(batch_id)["state"] == "target_succeeded"
    assert notifier.messages == []


def test_unrecoverable_errors_send_daily_critical_reminders(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    _write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4")
    notifier = RecordingNotifier([])
    collector = Collector(
        config,
        target_runners={"munchy": BrokenRunner("profile cannot preserve source artifacts")},
        notifier=notifier,
    )

    collector.run_once()
    batch_id = _single_batch_id(collector)
    collector.run_once()

    assert collector.load_batch(batch_id)["state"] == "failed_notified"
    assert notifier.messages == [
        f"{batch_id}:target:profile cannot preserve source artifacts",
    ]

    collector.run_once()
    assert len(notifier.messages) == 1

    with collector.connect() as conn:
        conn.execute(
            "UPDATE batches SET notified_error_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", batch_id),
        )

    collector.run_once()

    assert len(notifier.messages) == 2
    assert collector.load_batch(batch_id)["state"] == "failed_notified"


def test_munchy_payload_uses_structured_routing(tmp_path: Path) -> None:
    config = _base_config(tmp_path)
    _write_stable_file(tmp_path / "landing" / "camera" / "clip.mp4", b"camera data")
    _write_stable_file(tmp_path / "landing" / "camera" / "clip.xml", b"<meta />")
    collector = Collector(config, target_runners={"munchy": CompleteRunner()})

    collector.run_once()
    batch_id = _single_batch_id(collector)
    collector.move_batch_files(batch_id)
    collector.ensure_hashes(batch_id)
    request = munchy_upload_request(
        collector,
        batch_id,
        config.targets["runner"],
    )

    assert [item.rel_path for item in request.files] == ["camera/clip.mp4", "camera/clip.xml"]
    assert request.storage_hint == {
        "workflow_mode": "archive",
        "structured_routing": True,
        "groups": {
            "video": {
                "archive_mode": "av1_nvenc",
                "gpu_tasks": ["archive_video"],
            },
            "passthrough": {
                "archive_mode": "passthrough",
                "gpu_tasks": [],
            },
        },
    }
    assert request.job_payload["groups"]["video"]["encode_profile"]["archive"]["quality"] == 38
    assert request.job_payload["groups"]["passthrough"]["archive_mode"] == "passthrough"
    assert request.job_payload["profile_routing"]["routes"][0]["group"] == "video"


def test_cleanup_after_success_requires_safe_munchy_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot cleanup until Munchy waits for safe delete"):
        _base_config(tmp_path, cleanup="after_target_success", wait_for_safe_delete=False)


def test_jeb_only_accepts_munchy_targets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported type 'riverhog'"):
        config_from_mapping(
            {
                "collector": {
                    "state_db": str(tmp_path / "state.sqlite3"),
                    "batch_dir": str(tmp_path / "batches"),
                },
                "targets": {
                    "archive": {
                        "type": "riverhog",
                        "url": "http://riverhog.test",
                    }
                },
                "sources": [{"id": "camera", "path": str(tmp_path / "landing")}],
                "collections": [
                    {
                        "id": "weekly-device-artifacts",
                        "collection_slug": "weekly-device-artifacts",
                        "target": "archive",
                        "sources": ["camera"],
                    }
                ],
                "profile_groups": {"video": {}},
                "munchy_job_defaults": {
                    "profile_routing": {"routes": [{"id": "r", "group": "video"}]}
                },
            }
        )


def test_config_requires_munchy_profile_routing(tmp_path: Path) -> None:
    raw = {
        "targets": {"runner": {"type": "munchy", "url": "http://runner.test"}},
        "sources": [{"id": "camera", "path": str(tmp_path / "landing")}],
        "collections": [
            {
                "id": "weekly-device-artifacts",
                "collection_slug": "weekly-device-artifacts",
                "target": "runner",
                "sources": ["camera"],
            }
        ],
        "profile_groups": {"video": {}},
        "munchy_job_defaults": {},
    }

    with pytest.raises(ValueError, match="munchy_job_defaults.profile_routing is required"):
        config_from_mapping(raw)
