from __future__ import annotations

from pathlib import Path

import pytest
from config_validation import ConfigError
from gogurt_core.mounts import GogurtRouteMarker
from gogurt_path_volume_support import PATH_MARKER_NAME, PathMountedVolumeAccess


def test_path_provider_owns_the_complete_route_line_representation(tmp_path: Path) -> None:
    provider = PathMountedVolumeAccess(lambda: (tmp_path,))
    document = GogurtRouteMarker("camera")

    published = provider.publish_marker(tmp_path, document)
    restarted = PathMountedVolumeAccess(lambda: (tmp_path,)).observe_marker(tmp_path)

    assert (tmp_path / PATH_MARKER_NAME).read_bytes() == b"camera\n"
    assert published.marker == document
    assert restarted == published


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
    first = provider.publish_marker(tmp_path, GogurtRouteMarker("camera"))
    (tmp_path / PATH_MARKER_NAME).write_bytes(b"audio\n")
    second = provider.observe_marker(tmp_path)

    assert second is not None
    assert second.marker == GogurtRouteMarker("audio")
    assert second.identity != first.identity
