from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from gogurt_core.mounts import MountedMarkerSnapshot
from gogurt_core.providers import GogurtProviderReference
from gogurt_path_volume_support import PathMountedVolumeAccess


@dataclass(frozen=True, slots=True)
class FixtureMountedVolumeProvider:
    reference: GogurtProviderReference
    access: PathMountedVolumeAccess

    def discover(self) -> Sequence[Path]:
        return self.access.discover()

    def observe_marker(
        self,
        mount_point: Path,
        marker_name: str,
        *,
        max_bytes: int,
    ) -> MountedMarkerSnapshot | None:
        return self.access.observe_marker(mount_point, marker_name, max_bytes=max_bytes)

    def publish_marker(
        self,
        mount_point: Path,
        marker_name: str,
        content: bytes,
    ) -> MountedMarkerSnapshot:
        return self.access.publish_marker(mount_point, marker_name, content)


def path_mounted_volume_provider(
    discover: Callable[[], Sequence[Path]] = tuple,
    *,
    name: str = "test-path-volume",
    provider_id: str = "test-path-mounted-volume-provider/v1",
) -> FixtureMountedVolumeProvider:
    return FixtureMountedVolumeProvider(
        reference=GogurtProviderReference(
            kind="mounted-volume",
            name=name,
            provider_id=provider_id,
        ),
        access=PathMountedVolumeAccess(discover),
    )
