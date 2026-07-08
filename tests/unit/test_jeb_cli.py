from __future__ import annotations

import os
import time
from pathlib import Path

from jeb.cli import main as jeb_main
from jeb.collector import Collector, config_from_env


def jeb_env(tmp_path: Path) -> dict[str, str]:
    return {
        "JEB_ACCOUNTS": "phone",
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
    write_stable_file(tmp_path / "landing" / "phone" / "note.txt")

    assert jeb_main(["archive-now", "--account", "phone", "--no-process"]) == 0

    assert "archive attempt started for account phone" in capsys.readouterr().out
    collector = Collector(config_from_env(env))
    collector.init_db()
    [batch_id] = collector.active_batch_ids()
    assert [row["target_path"] for row in collector.batch_files(batch_id)] == ["phone/note.txt"]


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

    assert jeb_main(["check-config"]) == 0

    assert "ok: 1 sources" in capsys.readouterr().out
