"""Exact rclone review-delivery effect target."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue
from riverhog_protocol import ArtifactDisposition, canonical_json_bytes
from riverhog_transform_sdk import TransformWorkspace
from stove0_review_target_contracts import REVIEW_RCLONE_DELIVER_OPERATION
from stove0_review_target_support import (
    ReviewTargetServiceBase,
    SamplerRegistration,
    file_identity,
    review_options_schema,
)
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    OutputArtifact,
    TargetCollectionPublication,
    TargetEffectCommitUncertain,
    TargetExecutionRuntime,
    TargetJobRequest,
    TargetJobStatus,
)

EFFECT_OPTIONS = review_options_schema(
    "stove0.review-rclone-effect-target-options/v1",
    required=("destination_identity",),
    properties={
        "destination_identity": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
)


@dataclass(frozen=True, slots=True)
class RcloneReviewDestination:
    """Deployment-owned rclone destination for one fixed effect target."""

    identity: str
    remote: str
    config_path: Path | None = None
    executable: str = "rclone"
    timeout_seconds: int = 86400

    def __post_init__(self) -> None:
        if len(self.identity) != 64 or any(
            value not in "0123456789abcdef" for value in self.identity
        ):
            raise ValueError("review destination identity must be a lowercase SHA-256")
        if not self.remote or self.remote != self.remote.strip():
            raise ValueError("review rclone destination must be nonempty and canonical")
        if not self.executable or self.executable != self.executable.strip():
            raise ValueError("review rclone executable must be nonempty and canonical")
        if self.config_path is not None and not self.config_path.is_absolute():
            raise ValueError("review rclone config path must be absolute")
        if self.timeout_seconds < 1:
            raise ValueError("review rclone timeout must be positive")

    def commit(
        self,
        *,
        delivery_id: str,
        output_root: Path,
        artifacts: Sequence[OutputArtifact],
        manifest_path: Path,
    ) -> dict[str, JsonValue]:
        """Commit exact review objects, then publish their manifest marker last."""

        destination = f"{self.remote.rstrip('/')}/{delivery_id}"
        common = [self.executable]
        if self.config_path is not None:
            common.extend(("--config", str(self.config_path)))
        try:
            subprocess.run(
                [*common, "copy", str(output_root), f"{destination}/objects"],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
            subprocess.run(
                [*common, "copyto", str(manifest_path), f"{destination}/manifest.json"],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise TargetEffectCommitUncertain(
                "review delivery may have committed; inspect the configured destination"
            ) from exc
        _manifest_bytes, archive_root_sha256 = file_identity(manifest_path)
        return {
            "format": "stove0-review-rclone-receipt/v1",
            "destination_identity": self.identity,
            "delivery_id": delivery_id,
            "artifact_archive_root_sha256": archive_root_sha256,
            "artifact_count": len(artifacts),
            "total_bytes": sum(item.bytes for item in artifacts),
        }


class ReviewRcloneEffectTargetService(ReviewTargetServiceBase):
    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        samplers: tuple[SamplerRegistration, ...],
        destination: RcloneReviewDestination,
        source_revision: str = "unknown",
        image_digest: str,
        implementation_version: str,
        terminal_state_retention_seconds: int = DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    ) -> None:
        self.destination = destination
        super().__init__(
            state_root=state_root,
            workspace_root=workspace_root,
            samplers=samplers,
            source_revision=source_revision,
            image_digest=image_digest,
            implementation_version=implementation_version,
            protocol="stove0-effect-target/v1",
            implementation_id="stove0.review-rclone-effect-target/v1",
            operation=REVIEW_RCLONE_DELIVER_OPERATION,
            options_schema=EFFECT_OPTIONS,
            terminal_state_retention_seconds=terminal_state_retention_seconds,
        )

    def _fixed_target_options(self) -> dict[str, JsonValue]:
        return {"destination_identity": self.destination.identity}

    def _open_collection_publication(
        self,
        execution: TargetExecutionRuntime,
    ) -> None:
        del execution
        return None

    def _finish_publication(
        self,
        *,
        execution: TargetExecutionRuntime,
        workspace: TransformWorkspace,
        request: TargetJobRequest,
        publication: TargetCollectionPublication | None,
        artifacts: tuple[OutputArtifact, ...],
        dispositions: tuple[ArtifactDisposition, ...],
        execution_sha256: str,
        attempt: int,
        runtime_evidence: dict[str, JsonValue],
    ) -> TargetJobStatus:
        del dispositions
        if publication is not None:
            raise RuntimeError("review effect target unexpectedly opened a collection publication")
        manifest_path = workspace.resolve("control/delivery-manifest.json")
        manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest_path.write_bytes(
            canonical_json_bytes(
                {
                    "format": "stove0-review-delivery-manifest/v1",
                    "delivery_id": request.declaration.job_id,
                    "artifacts": [item.model_dump(mode="json") for item in artifacts],
                }
            )
        )
        result = self.destination.commit(
            delivery_id=request.declaration.job_id,
            output_root=workspace.resolve("output"),
            artifacts=artifacts,
            manifest_path=manifest_path,
        )
        return execution.effect_success(
            result,
            operation=REVIEW_RCLONE_DELIVER_OPERATION,
            execution_sha256=execution_sha256,
            attempt=attempt,
            runtime_evidence=runtime_evidence,
        )


__all__ = ["EFFECT_OPTIONS", "RcloneReviewDestination", "ReviewRcloneEffectTargetService"]
