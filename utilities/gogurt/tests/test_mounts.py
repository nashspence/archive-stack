from __future__ import annotations

from pathlib import Path

import pytest
from gogurt_core.mounts import (
    MAX_GOGURT_INTERVAL_SECONDS,
    MIN_GOGURT_INTERVAL_SECONDS,
    iter_new_mounts,
    validate_gogurt_interval,
)
from gogurt_linux_mounted_volume import (
    MOUNTED_VOLUME_PROVIDER_BINDING as LINUX_MOUNTED_VOLUME_PROVIDER,
)
from gogurt_linux_mounted_volume import linux_mount_points
from gogurt_macos_mounted_volume import (
    MOUNTED_VOLUME_PROVIDER_BINDING as MACOS_MOUNTED_VOLUME_PROVIDER,
)
from gogurt_macos_mounted_volume import macos_mount_points
from gogurt_windows_mounted_volume import (
    MOUNTED_VOLUME_PROVIDER_BINDING as WINDOWS_MOUNTED_VOLUME_PROVIDER,
)
from gogurt_windows_mounted_volume import windows_mount_points


def test_reference_provider_identities_seal_the_path_route_line_representation() -> None:
    assert {
        LINUX_MOUNTED_VOLUME_PROVIDER.provider_id,
        MACOS_MOUNTED_VOLUME_PROVIDER.provider_id,
        WINDOWS_MOUNTED_VOLUME_PROVIDER.provider_id,
    } == {
        "gogurt-linux-path-route-line-provider/v1",
        "gogurt-macos-path-route-line-provider/v1",
        "gogurt-windows-path-route-line-provider/v1",
    }


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


def test_windows_mount_discovery_uses_native_presence_not_path_accessibility() -> None:
    mask = (1 << (ord("C") - ord("A"))) | (1 << (ord("F") - ord("A")))
    assert windows_mount_points(lambda: mask) == (
        Path("C:\\"),
        Path("F:\\"),
    )


def test_linux_mount_presence_does_not_require_path_accessibility(tmp_path: Path) -> None:
    unavailable = tmp_path / "unavailable"
    mountinfo = f"24 1 8:1 / {unavailable} rw - ext4 /dev/sda1 rw\n"

    assert linux_mount_points(mountinfo) == (unavailable,)


def test_macos_mount_presence_does_not_require_path_accessibility(tmp_path: Path) -> None:
    volume = tmp_path / "camera"
    volume.touch()

    assert macos_mount_points(tmp_path) == (Path("/"), volume)


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


def test_polling_interval_is_finite_and_bounded() -> None:
    assert validate_gogurt_interval(MIN_GOGURT_INTERVAL_SECONDS) == 0.1
    assert validate_gogurt_interval(MAX_GOGURT_INTERVAL_SECONDS) == 3600.0

    for value in (True, "2", float("nan"), float("inf"), 0.09, 3600.01):
        with pytest.raises(ValueError):
            validate_gogurt_interval(value)
