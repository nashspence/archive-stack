"""Exact review-collection materialization target."""

from __future__ import annotations

from pathlib import Path

from pydantic import JsonValue
from riverhog_transform_sdk import TransformWorkspace
from stove0_review_target_contracts import REVIEW_MATERIALIZE_OPERATION
from stove0_review_target_support import (
    ReviewTargetServiceBase,
    SamplerRegistration,
    review_options_schema,
)
from stove0_target_support import (
    DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    OutputArtifact,
    TargetCollectionPublication,
    TargetExecutionRuntime,
    TargetJobRequest,
    TargetJobStatus,
)

MATERIALIZE_OPTIONS = review_options_schema("stove0.review-materialize-target-options/v1")


class ReviewMaterializeTargetService(ReviewTargetServiceBase):
    def __init__(
        self,
        *,
        state_root: Path,
        workspace_root: Path,
        samplers: tuple[SamplerRegistration, ...],
        source_revision: str = "unknown",
        image_digest: str,
        implementation_version: str,
        terminal_state_retention_seconds: int = DEFAULT_TERMINAL_STATE_RETENTION_SECONDS,
    ) -> None:
        super().__init__(
            state_root=state_root,
            workspace_root=workspace_root,
            samplers=samplers,
            source_revision=source_revision,
            image_digest=image_digest,
            implementation_version=implementation_version,
            protocol="stove0-transform-target/v1",
            implementation_id="stove0.review-materialize-target/v1",
            operation=REVIEW_MATERIALIZE_OPERATION,
            options_schema=MATERIALIZE_OPTIONS,
            terminal_state_retention_seconds=terminal_state_retention_seconds,
        )

    def _open_collection_publication(
        self,
        execution: TargetExecutionRuntime,
    ) -> TargetCollectionPublication:
        return execution.open_collection_publication()

    def _finish_publication(
        self,
        *,
        execution: TargetExecutionRuntime,
        workspace: TransformWorkspace,
        request: TargetJobRequest,
        publication: TargetCollectionPublication | None,
        artifacts: tuple[OutputArtifact, ...],
        execution_sha256: str,
        attempt: int,
        runtime_evidence: dict[str, JsonValue],
    ) -> TargetJobStatus:
        del execution, workspace, request, artifacts
        if publication is None:
            raise RuntimeError("review materialization publication was not initialized")
        return publication.finish_success(
            operation=REVIEW_MATERIALIZE_OPERATION,
            execution_sha256=execution_sha256,
            attempt=attempt,
            runtime_evidence=runtime_evidence,
        )


__all__ = ["MATERIALIZE_OPTIONS", "ReviewMaterializeTargetService"]
