from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import munchy_core.services.collection_transforms as module
from munchy_core.services.collection_transforms import (
    MunchyCollectionTransformService,
    TargetCollectionRequest,
    TargetCollectionResult,
)
from riverhog_api_client.producer import ProducedCollection, ProducerFile
from riverhog_protocol.collection_workflows import (
    ArtifactDisposition,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
    TransformIntent,
)


class Store:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def load(self, job_id: str) -> dict[str, Any] | None:
        value = self.values.get(job_id)
        return None if value is None else dict(value)

    def save(self, job: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(job)
        self.values[str(value["job_id"])] = value
        return dict(value)


class Target:
    def __init__(self, output: Path, *, fail: bool = False) -> None:
        self.output = output
        self.fail = fail
        self.released: list[str] = []

    def execute(self, request: TargetCollectionRequest) -> TargetCollectionResult:
        if self.fail:
            raise RuntimeError("deterministic malformed input")
        return TargetCollectionResult(
            outputs=(ProducerFile(self.output, "video/output.mkv"),),
            plan_sha256="c" * 64,
            execution_sha256="d" * 64,
            dispositions=(
                ArtifactDisposition(
                    input_collection_id=request.intent.inputs[0].collection_id,
                    input_manifest_sha256=request.intent.inputs[0].manifest_sha256,
                    input_path="camera/input.mov",
                    status="transformed",
                    outputs=("video/output.mkv",),
                ),
            ),
            provenance_journals={},
            source_context={"target": "fixture"},
        )

    def release(self, job_id: str) -> None:
        self.released.append(job_id)


class Api:
    def __enter__(self) -> Api:
        return self

    def __exit__(self, *_args: object) -> None:
        pass


class Producer:
    def __init__(self, _api: object, **_kwargs: object) -> None:
        pass

    def publish(self, files: object, **kwargs: object) -> ProducedCollection:
        assert files
        evidence = kwargs["inline_evidence"]
        assert "riverhog/collection-derivation.json" in evidence  # type: ignore[operator]
        return ProducedCollection(44, "e" * 64, "f" * 64, {"state": "finalized"})


def intent() -> TransformIntent:
    return TransformIntent.seal(
        recipe=RecipeIdentity("camera/v1", 1, "a" * 64),
        operation=OperationIdentity("archive-video/v1", "b" * 64),
        inputs=(CollectionRootIdentity(1, "1" * 64, "2" * 64),),
        effective_intent={"container": "mkv"},
        output_tags=("archive/camera",),
    )


def test_success_is_exactly_one_finalized_collection(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "output.mkv"
    output.write_bytes(b"data")
    store = Store()
    target = Target(output)
    monkeypatch.setattr(module, "CollectionProducer", Producer)
    service = MunchyCollectionTransformService(
        target=target,
        store=store,
        riverhog_api_factory=lambda _token: Api(),  # type: ignore[arg-type]
    )
    document = intent()
    service.create_or_resume(
        job_id=document.transform_id,
        claim_id=document.transform_id,
        fence=1,
        capability_token="scoped",
        intent=document.as_dict(),
    )

    result = service.run(document.transform_id)

    assert result["state"] == "succeeded"
    assert result["output_collection_id"] == 44
    assert result["derivation"]["transform_id"] == document.transform_id  # type: ignore[index]
    assert target.released == [document.transform_id]
    assert "capability_token" not in result
    assert all("capability_token" not in value for value in store.values.values())


def test_failure_produces_no_collection(tmp_path: Path) -> None:
    output = tmp_path / "unused"
    store = Store()
    target = Target(output, fail=True)
    service = MunchyCollectionTransformService(
        target=target,
        store=store,
        riverhog_api_factory=lambda _token: Api(),  # type: ignore[arg-type]
    )
    document = intent()
    service.create_or_resume(
        job_id=document.transform_id,
        claim_id=document.transform_id,
        fence=1,
        capability_token="scoped",
        intent=document.as_dict(),
    )

    result = service.run(document.transform_id)

    assert result["state"] == "failed"
    assert "output_collection_id" not in result
    assert target.released == [document.transform_id]


def test_restart_requires_idempotent_capability_refresh(tmp_path: Path) -> None:
    output = tmp_path / "output.mkv"
    output.write_bytes(b"data")
    store = Store()
    document = intent()
    first = MunchyCollectionTransformService(
        target=Target(output),
        store=store,
        riverhog_api_factory=lambda _token: Api(),  # type: ignore[arg-type]
    )
    first.create_or_resume(
        job_id=document.transform_id,
        claim_id=document.transform_id,
        fence=1,
        capability_token="scoped",
        intent=document.as_dict(),
    )

    restarted = MunchyCollectionTransformService(
        target=Target(output),
        store=store,
        riverhog_api_factory=lambda _token: Api(),  # type: ignore[arg-type]
    )
    waiting = restarted.run(document.transform_id)
    assert waiting["state"] == "queued"
    assert waiting["phase"] == "waiting_for_capability"

    resumed = restarted.create_or_resume(
        job_id=document.transform_id,
        claim_id=document.transform_id,
        fence=1,
        capability_token="replacement",
        intent=document.as_dict(),
    )
    assert resumed["phase"] == "queued"
    assert all("replacement" not in repr(value) for value in store.values.values())
