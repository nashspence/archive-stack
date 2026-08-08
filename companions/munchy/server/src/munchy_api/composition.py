"""Munchy server adapter composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class RiverhogAdapterHealth(Protocol):
    enabled: bool

    @property
    def background_running(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class MunchyAdapters:
    command_enabled: bool
    rclone_enabled: bool
    riverhog: RiverhogAdapterHealth


def configure_adapters() -> MunchyAdapters:
    import munchy_core.services.handoffs as handoff_service
    import munchy_core.services.media as media_service
    from munchy_core.adapters import external, gpu, riverhog

    handoff_service.register_handoff_adapter(
        external.ExternalHandoffAdapter("command"),
        option_model=external.CommandHandoffOptions,
    )
    handoff_service.register_handoff_adapter(
        external.ExternalHandoffAdapter("rclone"),
        option_model=external.RcloneHandoffOptions,
    )
    media_service.register_gpu_platform(gpu.HttpGpuPlatform())
    riverhog_adapter = riverhog.RiverhogHandoffAdapter()
    handoff_service.register_handoff_adapter(
        riverhog_adapter,
        option_model=riverhog.RiverhogHandoffOptions,
    )
    return MunchyAdapters(
        command_enabled=external.EXTERNAL_HANDOFF_ENABLED
        and bool(external.COMMAND_HANDOFF_COMMAND),
        rclone_enabled=external.EXTERNAL_HANDOFF_ENABLED and bool(external.RCLONE_HANDOFF_COMMAND),
        riverhog=riverhog_adapter,
    )
