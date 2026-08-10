from __future__ import annotations

from pathlib import Path

from gogurt.mounts import (
    iter_new_mounts,
    linux_mount_points,
    macos_mount_points,
    windows_mount_points,
)


def test_linux_mount_discovery_decodes_mountinfo_paths(tmp_path: Path) -> None:
    media = tmp_path / "Camera Card"
    media.mkdir()
    encoded = str(media).replace(" ", r"\040")
    mountinfo = f"24 1 8:1 / {encoded} rw - ext4 /dev/sda1 rw\n"

    assert linux_mount_points(mountinfo) == (media,)


def test_macos_mount_discovery_includes_root_and_mounted_volumes(tmp_path: Path) -> None:
    (tmp_path / "Camera").mkdir()
    (tmp_path / "Audio").mkdir()

    assert macos_mount_points(tmp_path) == (Path("/"), tmp_path / "Audio", tmp_path / "Camera")


def test_windows_mount_discovery_enumerates_present_drive_roots() -> None:
    assert windows_mount_points(lambda value: value in {"C:\\", "F:\\"}) == (
        Path("C:\\"),
        Path("F:\\"),
    )


def test_mount_watcher_reports_only_new_mounts() -> None:
    snapshots = iter(
        [
            [Path("/existing")],
            [Path("/existing"), Path("/camera")],
            [Path("/existing"), Path("/camera"), Path("/audio")],
        ]
    )
    events = iter_new_mounts(discover=lambda: next(snapshots), sleep=lambda _interval: None)

    assert next(events) == Path("/camera")
    assert next(events) == Path("/audio")


def test_mount_watcher_can_include_existing_mounts() -> None:
    events = iter_new_mounts(
        discover=lambda: [Path("/camera")],
        include_existing=True,
        sleep=lambda _interval: None,
    )

    assert next(events) == Path("/camera")
