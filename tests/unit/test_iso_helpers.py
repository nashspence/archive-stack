from __future__ import annotations

import subprocess
from pathlib import Path

from arc_core.iso.streaming import (
    ISO_BLOCK_BYTES,
    IsoEntry,
    IsoVolume,
    _parse_print_size_blocks,
    build_iso_cmd,
    build_iso_cmd_from_root,
    build_iso_print_size_cmd_from_root,
    estimate_iso_size_from_root,
)


def test_build_iso_cmd_contains_maps(tmp_path: Path) -> None:
    left = tmp_path / "left.txt"
    right = tmp_path / "right.bin"
    left.write_text("a")
    right.write_bytes(b"b")

    cmd = build_iso_cmd(
        IsoVolume(
            volume_id="VOL_001",
            filename="image.iso",
            entries=[
                IsoEntry(iso_path="/docs/left.txt", disk_path=left),
                IsoEntry(iso_path="/payload/right.bin", disk_path=right),
            ],
        )
    )

    assert "-volid" in cmd
    assert "/docs/left.txt" in cmd
    assert "/payload/right.bin" in cmd


def test_build_iso_cmd_from_root_maps_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    cmd = build_iso_cmd_from_root(image_root=root, volume_id="VOL_ROOT")
    assert cmd[-3:] == [str(root), "/", "-commit"]


def test_build_print_size_cmd_from_root_reuses_streaming_flags(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stream_cmd = build_iso_cmd_from_root(image_root=root, volume_id="VOL_ROOT")
    size_cmd = build_iso_print_size_cmd_from_root(image_root=root, volume_id="VOL_ROOT")

    assert size_cmd[:-2] == stream_cmd[:-1]
    assert size_cmd[-2:] == ["-print-size", "-end"]


def test_parse_print_size_accepts_size_prefix() -> None:
    assert _parse_print_size_blocks("xorriso : NOTE : foo\nsize=1234\n") == 1234


def test_estimate_iso_size_from_root_converts_blocks_to_bytes(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def fake_run(
        cmd: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert "-print-size" in cmd
        assert cmd[-2:] == ["-print-size", "-end"]
        return subprocess.CompletedProcess(cmd, 0, stdout="size=4321\n", stderr="")

    monkeypatch.setattr("arc_core.iso.streaming.subprocess.run", fake_run)
    used = estimate_iso_size_from_root(image_root=root, volume_id="VOL_ROOT", fallback_bytes=77)
    assert used == 4321 * ISO_BLOCK_BYTES


def test_estimate_iso_size_from_root_falls_back_if_xorriso_missing(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def fake_run(
        cmd: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr("arc_core.iso.streaming.subprocess.run", fake_run)
    used = estimate_iso_size_from_root(image_root=root, volume_id="VOL_ROOT", fallback_bytes=77)
    assert used == 77
