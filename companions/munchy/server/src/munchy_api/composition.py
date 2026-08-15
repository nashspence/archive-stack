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
    from munchy_core.adapters import external, riverhog, transform_targets
    from munchy_core.runtime import config as runtime_config

    handoff_service.register_handoff_adapter(
        external.ExternalHandoffAdapter("command"),
        option_model=external.CommandHandoffOptions,
    )
    handoff_service.register_handoff_adapter(
        external.ExternalHandoffAdapter("rclone"),
        option_model=external.RcloneHandoffOptions,
    )
    local_audio = media_service.LocalAudioTransformTarget(runtime_config.TRANSFORM_RUNTIME_DIR)
    local_audio_contract = local_audio.contract()
    media_service.register_transform_target_platform(
        transform_targets.HttpTransformTargetPlatform(
            in_process={
                "munchy-audio": transform_targets.InProcessTargetRegistration(
                    registration_id="munchy-audio",
                    target=local_audio,
                    workspace_root=runtime_config.TRANSFORM_RUNTIME_DIR,
                    expected_target_contract_sha256=local_audio_contract.contract_sha256,
                )
            }
        )
    )
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
