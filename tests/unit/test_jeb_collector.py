from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from jeb.collector import (
    Collector,
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
        "JEB_SCHEDULE": "always",
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


def test_munchy_payload_uses_account_group_paths_without_routing(tmp_path: Path) -> None:
    config = config_from_env(env_for(tmp_path, accounts="camera"))
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
    assert "profile_routing" not in request.job_payload
