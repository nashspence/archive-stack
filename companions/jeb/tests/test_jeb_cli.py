from __future__ import annotations

import json
import os
import time
from pathlib import Path

import jeb.collector as collector_module
import pytest
from jeb.collector import Collector, config_from_env
from jeb.service_cli import main as jeb_main


def jeb_env(tmp_path: Path, *, sources: str = "phone") -> dict[str, str]:
    return {
        "TEST_SOURCE_IDS": sources,
        "JEB_LANDING_DIR": str(tmp_path / "landing"),
        "JEB_STATE_DIR": str(tmp_path / "state"),
        "JEB_MUNCHY_URL": "http://munchy.invalid",
    }


def enroll(env: dict[str, str]) -> dict[str, str]:
    runtime_env = {key: value for key, value in env.items() if not key.startswith("TEST_")}
    collector = Collector(config_from_env(runtime_env))
    for source_id in env["TEST_SOURCE_IDS"].split(","):
        collector.add_source(
            source_id,
            adapters=("tus",),
            target_config={"template_id": "camera-archive"},
            credential=f"{source_id}-password",
            stable_seconds=0,
            include_extensions=(".txt",),
        )
    return runtime_env


@pytest.fixture(autouse=True)
def accept_target_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMunchyClient:
        def __init__(self, url: str, *, token: str = "") -> None:
            self.url = url
            self.token = token

        def preflight_submission(self, request: object) -> dict[str, object]:
            _ = request
            return {"accepted": True}

        def close(self) -> None:
            pass

    monkeypatch.setattr(collector_module, "MunchyClient", FakeMunchyClient)


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
    env = enroll(jeb_env(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")

    assert jeb_main(["archive-now", "--source", "phone", "--no-process"]) == 0

    output = capsys.readouterr().out
    assert "jeb archive" in output
    assert "jeb archive: staged" in output
    collector = Collector(config_from_env(env))
    collector.init_db()
    [batch_id] = collector.active_attempt_ids()
    assert [row["target_path"] for row in collector.attempt_files(batch_id)] == ["phone/note.txt"]


def test_jeb_archive_now_dry_run_reports_plan_without_batch(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = enroll(jeb_env(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")

    assert jeb_main(["archive-now", "--source", "phone", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "Jeb archive plan: phone" in output
    assert "eligible files: 1" in output
    collector = Collector(config_from_env(env))
    collector.init_db()
    assert collector.active_attempt_ids() == []
    assert collector.list_attempts(terminal="all")["total"] == 0


def test_jeb_archive_now_reports_unknown_source_concisely(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = enroll(jeb_env(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert jeb_main(["archive-now", "--source", "missing", "--no-process"]) == 1

    captured = capsys.readouterr()
    assert "source 'missing' is not enrolled" in captured.err


def test_jeb_check_config_reads_env(tmp_path: Path, capsys, monkeypatch) -> None:
    env = jeb_env(tmp_path)
    runtime_env = {key: value for key, value in env.items() if not key.startswith("TEST_")}
    for key, value in runtime_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")

    assert jeb_main(["check-config"]) == 0

    output = capsys.readouterr().out
    assert "Jeb config: ok" in output


def test_jeb_attempts_json_pages_sorts_and_filters(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = enroll(jeb_env(tmp_path, sources="phone,camera"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt", b"p")
    write_stable_file(tmp_path / "landing" / "camera" / "clip.txt", b"camera")
    assert jeb_main(["archive-now", "--source", "phone", "--no-process"]) == 0
    assert jeb_main(["archive-now", "--source", "camera", "--no-process"]) == 0
    capsys.readouterr()

    assert (
        jeb_main(
            [
                "attempt",
                "list",
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
    assert payload["attempts"][0]["source_id"] == "camera"
    assert payload["attempts"][0]["total_bytes"] == 6

    assert jeb_main(["attempt", "list", "--terminal", "all", "--source", "phone", "--json"]) == 0

    filtered = json.loads(capsys.readouterr().out)
    assert filtered["total"] == 1
    assert filtered["filters"]["source"] == "phone"
    assert filtered["attempts"][0]["source_id"] == "phone"

    assert jeb_main(["attempt", "list", "--terminal", "all", "--query", "camera", "--json"]) == 0

    queried = json.loads(capsys.readouterr().out)
    assert queried["total"] == 1
    assert queried["query"] == "camera"
    assert queried["attempts"][0]["source_id"] == "camera"

    assert jeb_main(["attempt", "list", "--terminal", "all", "--all", "--json"]) == 0

    all_attempts = json.loads(capsys.readouterr().out)
    assert all_attempts["page"] == 1
    assert all_attempts["per_page"] == 2
    assert all_attempts["pages"] == 1
    assert all_attempts["total"] == 2

    assert jeb_main(["attempt", "list", "--terminal", "all", "--all", "--ids"]) == 0

    ids = capsys.readouterr().out.splitlines()
    assert ids == [attempt["attempt_id"] for attempt in all_attempts["attempts"]]


def test_jeb_attempts_source_filter_treats_slug_as_literal(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = enroll(jeb_env(tmp_path, sources="front_door,frontxdoor"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    write_stable_file(tmp_path / "landing" / "front_door" / "note.txt", b"under")
    write_stable_file(tmp_path / "landing" / "frontxdoor" / "note.txt", b"plain")
    assert jeb_main(["archive-now", "--source", "front_door", "--no-process"]) == 0
    assert jeb_main(["archive-now", "--source", "frontxdoor", "--no-process"]) == 0
    capsys.readouterr()

    assert (
        jeb_main(["attempt", "list", "--terminal", "all", "--source", "front_door", "--json"]) == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["attempts"][0]["source_id"] == "front_door"


def test_jeb_status_json_reports_sources_backlog_and_active_attempts(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    env = enroll(jeb_env(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt", b"notes")
    assert jeb_main(["archive-now", "--source", "phone", "--no-process"]) == 0
    capsys.readouterr()

    assert jeb_main(["status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"][0]["id"] == "phone"
    assert payload["sources"][0]["eligible_files"] == 1
    assert payload["sources"][0]["eligible_bytes"] == 5
    assert payload["batches"]["active"] == 1
    assert payload["batches"]["states"] == {"batching": 1}
    assert payload["active_attempts"]["total"] == 1
    assert payload["active_attempts"]["attempts"][0]["state"] == "batching"
    assert payload["target_preflight_failures"]["total"] == 0
    assert payload["incomplete_tus_uploads"]["total"] == 0
