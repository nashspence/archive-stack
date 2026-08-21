from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from riverhog_storage_adapter_protocol import canonical_json_bytes
from riverhog_storage_adapter_support import (
    RECOVERY_EXPORT_FORMAT,
    RecoveryExportEntry,
    export_recovery_root,
    recovery_export_main,
)


class _Source:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def iter_recovery_export_entries(self) -> Iterator[RecoveryExportEntry]:
        for object_path, content in self.objects.items():
            yield RecoveryExportEntry(
                object_path=object_path,
                stored_bytes=len(content),
                source_ref=object_path,
            )

    def iter_recovery_export_content(
        self,
        entry: RecoveryExportEntry,
    ) -> Iterator[bytes]:
        content = self.objects[entry.source_ref]
        for offset in range(0, len(content), 3):
            yield content[offset : offset + 3]


def test_recovery_export_streams_exact_current_root_with_deterministic_evidence(
    tmp_path: Path,
) -> None:
    objects = {
        "archives/one.age": b"encrypted-one",
        "archives/two.age": b"encrypted-two",
        "metadata.json.age": b"encrypted-metadata",
    }
    destination = tmp_path / "export"

    report = export_recovery_root(_Source(objects), destination)

    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == objects
    expected_root = hashlib.sha256()
    for object_path, content in objects.items():
        expected_root.update(
            canonical_json_bytes(
                {
                    "object_path": object_path,
                    "stored_bytes": len(content),
                    "stored_sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        )
        expected_root.update(b"\n")
    assert report == {
        "format": RECOVERY_EXPORT_FORMAT,
        "objects": 3,
        "stored_bytes": sum(map(len, objects.values())),
        "root_sha256": expected_root.hexdigest(),
    }


def test_recovery_export_requires_canonical_order_and_an_empty_destination(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="canonically ordered"):
        export_recovery_root(
            _Source({"z.age": b"z", "a.age": b"a"}),
            tmp_path / "unordered",
        )

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "operator-file").write_bytes(b"owned")
    with pytest.raises(ValueError, match="must be empty"):
        export_recovery_root(_Source({"a.age": b"a"}), occupied)
    assert (occupied / "operator-file").read_bytes() == b"owned"


def test_recovery_export_rejects_symlink_destination(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        export_recovery_root(_Source({"a.age": b"a"}), link)


def test_recovery_export_command_emits_machine_readable_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "export"

    assert (
        recovery_export_main(
            lambda: _Source({"a.age": b"encrypted"}),
            prog="fixture-export",
            version="1.0.0",
            argv=[str(destination)],
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["format"] == RECOVERY_EXPORT_FORMAT
    assert report["objects"] == 1
    assert (destination / "a.age").read_bytes() == b"encrypted"


def test_recovery_export_help_and_version_do_not_construct_the_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def source():
        raise AssertionError("help and version must not touch provider configuration")

    with pytest.raises(SystemExit, match="0"):
        recovery_export_main(
            source,
            prog="fixture-export",
            version="1.0.0",
            argv=["--help"],
        )
    assert "usage" in capsys.readouterr().out.casefold()

    with pytest.raises(SystemExit, match="0"):
        recovery_export_main(
            source,
            prog="fixture-export",
            version="1.0.0",
            argv=["--version"],
        )
    assert capsys.readouterr().out.strip() == "1.0.0"
