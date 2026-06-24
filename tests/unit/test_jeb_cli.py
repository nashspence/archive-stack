from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

from jeb.cli import main as jeb_main
from jeb.collector import Collector, load_config


def write_jeb_config(tmp_path: Path) -> Path:
    landing = tmp_path / "landing"
    landing.mkdir()
    config_path = tmp_path / "jeb.toml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            [collector]
            state_db = "{tmp_path / 'state' / 'jeb.sqlite3'}"
            batch_dir = "{tmp_path / 'landing' / '.jeb-batches'}"

            [targets.munchy]
            type = "munchy"
            url = "http://munchy.invalid"

            [profile_groups.passthrough]
            archive_mode = "passthrough"

            [munchy_job_defaults.profile_routing]

            [[munchy_job_defaults.profile_routing.routes]]
            id = "passthrough-text"
            group = "passthrough"
            when = {{ path = {{ suffix = ".txt" }} }}

            [[sources]]
            id = "phone"
            enabled = true
            path = "{landing / 'phone'}"
            upload_prefix = "phone"
            stable_age = "0s"
            include_extensions = []

            [[collections]]
            id = "weekly"
            enabled = true
            collection_slug = "weekly"
            target = "munchy"
            cleanup = "never"
            schedule = "weekly"
            weekday = "monday"
            hour = 3
            minute = 0
            sources = ["phone"]
            """
        ).strip()
    )
    return config_path


def write_stable_file(path: Path, content: bytes = b"notes") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - 14 * 86_400
    os.utime(path, (old, old))


def test_jeb_archive_now_starts_batch_without_processing(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config_path = write_jeb_config(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")

    monkeypatch.setattr(
        "jeb.collector.MunchyRunnerClient.profile_routing_preflight",
        lambda self, **kwargs: {
            "ok": True,
            "files_total": 1,
            "matched_files": 1,
            "unmatched_files": 0,
            "matches": [{"path": "phone/note.txt", "route_id": "passthrough-text"}],
            "unmatched": [],
        },
    )

    assert (
        jeb_main(["--config", str(config_path), "archive-now", "--source", "phone", "--no-process"])
        == 0
    )

    assert "archive attempt started for source phone" in capsys.readouterr().out
    collector = Collector(load_config(config_path))
    collector.init_db()
    [batch_id] = collector.active_batch_ids()
    assert [row["target_path"] for row in collector.batch_files(batch_id)] == ["phone/note.txt"]


def test_jeb_archive_now_reports_failed_preflight(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config_path = write_jeb_config(tmp_path)
    write_stable_file(tmp_path / "landing" / "phone" / "IMG_0001.HEIC")

    monkeypatch.setattr(
        "jeb.collector.MunchyRunnerClient.profile_routing_preflight",
        lambda self, **kwargs: {
            "ok": False,
            "files_total": 1,
            "matched_files": 0,
            "unmatched_files": 1,
            "matches": [],
            "unmatched": [{"path": "phone/IMG_0001.HEIC", "reason": "no_matching_route"}],
        },
    )

    assert (
        jeb_main(["--config", str(config_path), "archive-now", "--source", "phone", "--no-process"])
        == 1
    )

    assert "routing preflight still failed" in capsys.readouterr().out
    collector = Collector(load_config(config_path))
    collector.init_db()
    assert collector.active_batch_ids() == []
    [failure] = collector.routing_preflight_failures(source_id="phone")
    assert failure["unmatched_count"] == 1
