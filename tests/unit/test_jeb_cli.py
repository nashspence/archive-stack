from __future__ import annotations

import json
import os
import time
from pathlib import Path

from jeb.collector import Collector, config_from_env
from jeb.service_cli import main as jeb_main


def jeb_env(tmp_path: Path, *, accounts: str = "phone") -> dict[str, str]:
    return {
        "JEB_ACCOUNTS": accounts,
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.invalid",
        "JEB_INCLUDE_EXTENSIONS": ".txt",
        "JEB_STABLE_AGE": "0s",
        "JEB_CADENCE": "weekly",
    }


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
    env = jeb_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")

    assert jeb_main(["archive-now", "--account", "phone", "--no-process"]) == 0

    output = capsys.readouterr().out
    assert "jeb archive" in output
    assert "status: staged" in output
    assert "account: phone" in output
    collector = Collector(config_from_env(env))
    collector.init_db()
    [batch_id] = collector.active_batch_ids()
    assert [row["target_path"] for row in collector.batch_files(batch_id)] == ["phone/note.txt"]


def test_jeb_archive_now_dry_run_reports_plan_without_batch(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = jeb_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")

    assert jeb_main(["archive-now", "--account", "phone", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "jeb archive dry-run" in output
    assert "status: would_process" in output
    assert "account: phone" in output
    collector = Collector(config_from_env(env))
    collector.init_db()
    assert collector.active_batch_ids() == []
    assert collector.list_batches(terminal="all")["total"] == 0


def test_jeb_archive_now_reports_removed_account_without_traceback(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = jeb_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert jeb_main(["archive-now", "--account", "missing", "--no-process"]) == 1

    captured = capsys.readouterr()
    assert "account 'missing' is not in the active Jeb env" in captured.err
    assert "Traceback" not in captured.err


def test_jeb_check_config_reads_env(tmp_path: Path, capsys, monkeypatch) -> None:
    env = jeb_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_main(["check-config"]) == 0

    output = capsys.readouterr().out
    assert "jeb config" in output
    assert "sources: 1" in output


def test_jeb_batches_json_pages_sorts_and_filters(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = jeb_env(tmp_path, accounts="phone,camera")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt", b"p")
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt", b"camera")
    assert jeb_main(["archive-now", "--account", "phone", "--no-process"]) == 0
    assert jeb_main(["archive-now", "--account", "camera", "--no-process"]) == 0
    capsys.readouterr()

    assert (
        jeb_main(
            [
                "batches",
                "--terminal",
                "all",
                "--sort",
                "bytes",
                "--order",
                "desc",
                "--per-page",
                "1",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["page"] == 1
    assert payload["per_page"] == 1
    assert payload["total"] == 2
    assert payload["pages"] == 2
    assert payload["sort"] == "bytes"
    assert payload["batches"][0]["accounts"] == ["camera"]
    assert payload["batches"][0]["total_bytes"] == 6

    assert jeb_main(["batches", "--terminal", "all", "--account", "phone", "--json"]) == 0

    filtered = json.loads(capsys.readouterr().out)
    assert filtered["total"] == 1
    assert filtered["filters"]["account"] == "phone"
    assert filtered["batches"][0]["accounts"] == ["phone"]

    assert jeb_main(["batches", "--terminal", "all", "--query", "camera", "--json"]) == 0

    queried = json.loads(capsys.readouterr().out)
    assert queried["total"] == 1
    assert queried["query"] == "camera"
    assert queried["batches"][0]["collection_id"] == "camera"


def test_jeb_batches_account_filter_treats_slug_as_literal(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = jeb_env(tmp_path, accounts="front_door,frontxdoor")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    write_stable_file(tmp_path / "landing" / "front_door" / "note.txt", b"under")
    write_stable_file(tmp_path / "landing" / "frontxdoor" / "note.txt", b"plain")
    assert jeb_main(["archive-now", "--account", "front_door", "--no-process"]) == 0
    assert jeb_main(["archive-now", "--account", "frontxdoor", "--no-process"]) == 0
    capsys.readouterr()

    assert (
        jeb_main(["batches", "--terminal", "all", "--account", "front_door", "--json"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["batches"][0]["accounts"] == ["front_door"]


def test_jeb_status_json_reports_sources_backlog_and_active_attempts(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = jeb_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt", b"notes")
    assert jeb_main(["archive-now", "--account", "phone", "--no-process"]) == 0
    capsys.readouterr()

    assert jeb_main(["status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"][0]["id"] == "phone"
    assert payload["sources"][0]["eligible_files"] == 1
    assert payload["sources"][0]["eligible_bytes"] == 5
    assert payload["batches"]["active"] == 1
    assert payload["batches"]["states"] == {"batching": 1}
    assert payload["active_attempts"]["total"] == 1
    assert payload["active_attempts"]["batches"][0]["state"] == "batching"
    assert payload["routing_preflight_failures"]["total"] == 0
