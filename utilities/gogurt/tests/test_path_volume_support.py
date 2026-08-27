from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from config_validation import ConfigError
from gogurt_core.mounts import GogurtRouteMarker
from gogurt_path_volume_support import (
    PATH_MARKER_NAME,
    PATH_MARKER_PUBLICATION_LOCK_NAME,
    PathMountedVolumeAccess,
)


def test_path_provider_owns_the_complete_route_line_representation(tmp_path: Path) -> None:
    provider = PathMountedVolumeAccess(lambda: (tmp_path,))
    document = GogurtRouteMarker("camera")

    published = provider.publish_marker(tmp_path, document, expected=None)
    restarted = PathMountedVolumeAccess(lambda: (tmp_path,)).observe_marker(tmp_path)

    assert (tmp_path / PATH_MARKER_NAME).read_bytes() == b"camera\n"
    assert published.marker == document
    assert restarted == published
    if os.name != "nt":
        lock_mode = stat.S_IMODE((tmp_path / PATH_MARKER_PUBLICATION_LOCK_NAME).stat().st_mode)
        assert lock_mode == 0o600


@pytest.mark.parametrize(
    "content",
    [b" camera\n", b"camera\nextra\n", b"\xff\n"],
)
def test_path_provider_rejects_noncanonical_physical_representations(
    tmp_path: Path,
    content: bytes,
) -> None:
    (tmp_path / PATH_MARKER_NAME).write_bytes(content)

    with pytest.raises(ConfigError):
        PathMountedVolumeAccess(tuple).observe_marker(tmp_path)


@pytest.mark.parametrize("content", [b"camera", b"camera\n", b"camera\r\n"])
def test_path_provider_accepts_portable_single_line_terminations(
    tmp_path: Path,
    content: bytes,
) -> None:
    (tmp_path / PATH_MARKER_NAME).write_bytes(content)

    observation = PathMountedVolumeAccess(tuple).observe_marker(tmp_path)

    assert observation is not None
    assert observation.marker == GogurtRouteMarker("camera")


def test_path_provider_identity_changes_with_relevant_physical_state(tmp_path: Path) -> None:
    provider = PathMountedVolumeAccess(tuple)
    first = provider.publish_marker(
        tmp_path,
        GogurtRouteMarker("camera"),
        expected=None,
    )
    (tmp_path / PATH_MARKER_NAME).write_bytes(b"audio\n")
    second = provider.observe_marker(tmp_path)

    assert second is not None
    assert second.marker == GogurtRouteMarker("audio")
    assert second.identity != first.identity


def test_path_provider_never_clobbers_an_unexpected_marker(tmp_path: Path) -> None:
    provider = PathMountedVolumeAccess(tuple)
    marker = tmp_path / PATH_MARKER_NAME
    marker.write_bytes(b"other\n")

    with pytest.raises(FileExistsError, match="appeared before publication"):
        provider.publish_marker(
            tmp_path,
            GogurtRouteMarker("camera"),
            expected=None,
        )

    assert marker.read_bytes() == b"other\n"


def test_path_provider_replacement_requires_the_exact_prior_observation(
    tmp_path: Path,
) -> None:
    provider = PathMountedVolumeAccess(tuple)
    marker = tmp_path / PATH_MARKER_NAME
    marker.write_bytes(b"other\n")
    expected = provider.observe_marker(tmp_path)
    assert expected is not None
    marker.write_bytes(b"changed\n")

    with pytest.raises(ConfigError, match="changed before publication"):
        provider.publish_marker(
            tmp_path,
            GogurtRouteMarker("camera"),
            expected=expected,
        )

    assert marker.read_bytes() == b"changed\n"
