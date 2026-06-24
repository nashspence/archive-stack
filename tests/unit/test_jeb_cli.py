from __future__ import annotations

import json
import textwrap
from pathlib import Path

from jeb.cli import main as jeb_main
from jeb.collector import CaptureSignature, Collector, load_config, stable_json


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
            suffixes = [".txt"]

            [[sources]]
            id = "phone"
            enabled = true
            path = "{landing / 'phone'}"
            upload_prefix = "phone"
            stable_age = "0s"
            include_extensions = []
            unmatched_policy = "hold"

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


def test_jeb_signatures_list_and_show_json(tmp_path: Path, capsys) -> None:
    config_path = write_jeb_config(tmp_path)
    collector = Collector(load_config(config_path))
    collector.init_db()
    with collector.connect() as conn:
        conn.execute(
            """
            INSERT INTO held_signatures(
                source_id, signature_id, state, reason, signature_json,
                example_paths_json, file_count, total_bytes, oldest_mtime_ns,
                newest_mtime_ns, first_seen_at, last_seen_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "phone",
                "abc123",
                "held",
                "no_matching_route",
                stable_json({"suffix": ".heic"}),
                stable_json(["phone/IMG_0001.HEIC"]),
                1,
                42,
                1_000_000_000,
                2_000_000_000,
                "2026-06-24T00:00:00Z",
                "2026-06-24T00:00:00Z",
                "2026-06-24T00:00:00Z",
            ),
        )

    assert jeb_main(["--config", str(config_path), "signatures", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["signature_id"] == "abc123"
    assert listed[0]["signature"] == {"suffix": ".heic"}
    assert listed[0]["example_paths"] == ["phone/IMG_0001.HEIC"]

    assert jeb_main(["signatures", "show", "abc123", "--config", str(config_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["source_id"] == "phone"
    assert shown["reason"] == "no_matching_route"


def test_jeb_signatures_probe_json(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    sample = tmp_path / "IMG_0001.HEIC"
    sample.write_bytes(b"not media")
    monkeypatch.setattr(
        "jeb.cli.safe_capture_signature_for_file",
        lambda path: CaptureSignature(id="sig1", data={"suffix": path.suffix.lower()}),
    )

    assert jeb_main(["signatures", "probe", str(sample), "--json"]) == 0

    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "path": str(sample),
            "signature_id": "sig1",
            "signature": {"suffix": ".heic"},
        }
    ]
