from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from riverhog_core.iso.streaming import (
    ISO_BLOCK_BYTES,
    STDOUT_STREAM_TRAILER_BLOCKS,
    IsoEntry,
    IsoVolume,
    _parse_print_size_blocks,
    build_iso_cmd,
    build_iso_cmd_from_root,
    build_iso_print_size_cmd_from_root,
    build_iso_validation_cmd,
    estimate_iso_size_from_root,
    stream_iso_from_root,
    validate_iso_image,
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


def test_stream_iso_from_root_with_known_length_sends_headers_before_body(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    reads = 0

    class FakeStream:
        async def read(self, _size: int) -> bytes:
            nonlocal reads
            reads += 1
            return b""

    class FakeStderr:
        async def read(self, _size: int) -> bytes:
            return b""

    class FakeProc:
        stdout = FakeStream()
        stderr = FakeStderr()
        returncode: int | None = None
        pid = 999999

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_create_subprocess_exec(*_args, **_kwargs) -> FakeProc:
        return FakeProc()

    async def run() -> None:
        stream = await stream_iso_from_root(
            image_root=root,
            volume_id="VOL_ROOT",
            filename="image.iso",
            content_length=123,
        )
        assert stream.headers is not None
        assert stream.headers["Content-Length"] == "123"
        assert reads == 0
        assert [chunk async for chunk in stream.body] == []
        assert reads == 1

    monkeypatch.setattr(
        "riverhog_core.iso.streaming.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    asyncio.run(run())


def test_build_print_size_cmd_from_root_uses_lightweight_streaming_flags(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stream_cmd = build_iso_cmd_from_root(image_root=root, volume_id="VOL_ROOT")
    size_cmd = build_iso_print_size_cmd_from_root(image_root=root, volume_id="VOL_ROOT")

    stream_outdev = stream_cmd.index("-outdev") + 1
    size_outdev = size_cmd.index("-outdev") + 1
    comparable_size_cmd = [*size_cmd]
    comparable_size_cmd[size_outdev] = stream_cmd[stream_outdev]
    comparable_stream_cmd = [*stream_cmd]
    md5_index = comparable_stream_cmd.index("-md5")
    del comparable_stream_cmd[md5_index : md5_index + 2]
    assert size_cmd[size_outdev].startswith("stdio:")
    assert "-md5" not in size_cmd
    assert comparable_size_cmd[:-2] == comparable_stream_cmd[:-1]
    assert size_cmd[-2:] == ["-print-size", "-end"]


def test_build_iso_validation_cmd_checks_embedded_md5s(tmp_path: Path) -> None:
    iso_path = tmp_path / "image.iso"
    cmd = build_iso_validation_cmd(iso_path)

    assert "-check_md5" in cmd
    assert "-check_md5_r" in cmd
    assert str(iso_path) in cmd


def test_validate_iso_image_raises_with_xorriso_detail(monkeypatch, tmp_path: Path) -> None:
    iso_path = tmp_path / "image.iso"

    def fake_run(
        cmd: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert str(iso_path) in cmd
        return subprocess.CompletedProcess(cmd, 32, stdout="", stderr="bad iso")

    monkeypatch.setattr("riverhog_core.iso.streaming.subprocess.run", fake_run)

    try:
        validate_iso_image(iso_path)
    except RuntimeError as exc:
        assert "bad iso" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected ISO validation failure")


def test_parse_print_size_accepts_size_prefix() -> None:
    assert _parse_print_size_blocks("xorriso : NOTE : foo\nsize=1234\n") == 1234


def test_parse_print_size_accepts_xorriso_image_size_line() -> None:
    assert _parse_print_size_blocks("Image size   : 186s\n") == 186


def test_estimate_iso_size_from_root_converts_blocks_to_bytes(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def fake_run(
        cmd: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert "-print-size" in cmd
        assert cmd[-2:] == ["-print-size", "-end"]
        return subprocess.CompletedProcess(cmd, 0, stdout="size=4321\n", stderr="")

    monkeypatch.setattr("riverhog_core.iso.streaming.subprocess.run", fake_run)
    used = estimate_iso_size_from_root(image_root=root, volume_id="VOL_ROOT", fallback_bytes=77)
    assert used == (4321 + STDOUT_STREAM_TRAILER_BLOCKS) * ISO_BLOCK_BYTES


def test_estimate_iso_size_from_root_falls_back_if_xorriso_missing(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    def fake_run(
        cmd: list[str], *, capture_output: bool, text: bool, check: bool
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr("riverhog_core.iso.streaming.subprocess.run", fake_run)
    used = estimate_iso_size_from_root(image_root=root, volume_id="VOL_ROOT", fallback_bytes=77)
    assert used == 77
