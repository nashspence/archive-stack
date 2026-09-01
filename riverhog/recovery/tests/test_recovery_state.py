from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from riverhog_archive_contracts import ArchiveFileIdentity, format_archive_sequence
from riverhog_provenance import format_provenance_sequence
from riverhog_recover._checkpoint_sha256 import CheckpointSHA256
from riverhog_recover.recovery import (
    _initialize_provenance_recovery_state,
    _initialize_recovery_state,
    _record_recovered_file,
    _save_provenance_progress,
)


def test_archive_recovery_checkpoint_round_trips_full_v1_sequence_domain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    values = (0, (1 << 63) - 1, 1 << 63, (1 << 256) - 1)
    with sqlite3.connect(database) as state:
        _initialize_recovery_state(state)
        state.executemany(
            "INSERT INTO volumes(sequence, metadata_sha256) VALUES (?, ?)",
            ((format_archive_sequence(value), "a" * 64) for value in reversed(values)),
        )
        state.execute(
            "INSERT INTO sequence_progress(name, next_sequence, hash_state) VALUES (?, ?, ?)",
            ("archive", format_archive_sequence(values[-1]), CheckpointSHA256().export_state()),
        )
        _record_recovered_file(
            state,
            ArchiveFileIdentity(path="large.bin", bytes=1, sha256="b" * 64),
            volume_sequence=values[-1],
            kind="segment",
        )
        state.commit()

    with sqlite3.connect(database) as restarted:
        assert [
            row[0] for row in restarted.execute("SELECT sequence FROM volumes ORDER BY sequence")
        ] == [format_archive_sequence(value) for value in values]
        assert restarted.execute(
            "SELECT next_sequence FROM sequence_progress WHERE name = 'archive'"
        ).fetchone() == (format_archive_sequence(values[-1]),)
        assert restarted.execute(
            "SELECT volume_sequence FROM file_volumes WHERE path = 'large.bin'"
        ).fetchone() == (format_archive_sequence(values[-1]),)


def test_recovery_checkpoint_rejects_noncanonical_sequence_storage() -> None:
    with sqlite3.connect(":memory:") as state:
        _initialize_recovery_state(state)
        for value in ("1", "G" * 64, "0" * 63 + "A"):
            with pytest.raises(sqlite3.IntegrityError):
                state.execute(
                    "INSERT INTO volumes(sequence, metadata_sha256) VALUES (?, ?)",
                    (value, "a" * 64),
                )


def test_provenance_recovery_terminal_supports_maximum_v1_sequence() -> None:
    maximum = (1 << 256) - 1
    with sqlite3.connect(":memory:") as state:
        _initialize_provenance_recovery_state(state)
        digest = CheckpointSHA256()
        state.execute(
            "INSERT INTO provenance_progress VALUES (1, ?, ?, 0, NULL, NULL, NULL, 0, '', 0, 0)",
            (format_provenance_sequence(0), digest.export_state()),
        )
        _save_provenance_progress(
            state,
            next_volume=maximum,
            volume_digest=digest,
            next_file_order=0,
            previous_path=None,
            previous_journal_id=None,
            current_journal_id=None,
            current_journal_bytes=0,
            current_journal_sha256="",
            journal_count=0,
            omitted=0,
            terminal_sequence=maximum,
        )

        assert state.execute(
            "SELECT next_volume FROM provenance_progress WHERE singleton = 1"
        ).fetchone() == (format_provenance_sequence(maximum),)
        assert state.execute(
            "SELECT sequence FROM provenance_terminal WHERE singleton = 1"
        ).fetchone() == (format_provenance_sequence(maximum),)
