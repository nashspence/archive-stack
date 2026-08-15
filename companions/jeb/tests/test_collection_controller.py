from __future__ import annotations

from typing import Any

from jeb_core.services.collection_controller import (
    CollectionBatchPolicy,
    CollectionTransformRecipe,
    JebCollectionController,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


class Riverhog:
    def __init__(self) -> None:
        self.collections: dict[int, dict[str, Any]] = {
            1: {
                "id": 1,
                "tags": ["intake/camera"],
                "bytes": 4,
                "manifest_sha256": SHA_A,
                "content_etag": SHA_B,
            },
            2: {
                "id": 2,
                "tags": ["intake/camera"],
                "bytes": 5,
                "manifest_sha256": SHA_C,
                "content_etag": SHA_D,
            },
        }

    def list_collections(self, **_kwargs: Any) -> dict[str, Any]:
        return {"collections": list(self.collections.values())}

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        return self.collections[collection_id]


class Munchy:
    pass


def recipe() -> CollectionTransformRecipe:
    return CollectionTransformRecipe(
        id="camera-archive/v1",
        revision=1,
        operation_id="archive-video/v1",
        operation_sha256="e" * 64,
        select_all_tags=("intake/camera",),
        output_tags=("archive/camera",),
        effective_intent={"container": "mkv"},
        batch=CollectionBatchPolicy(max_collections=1, max_bytes=100),
    )


def test_planning_is_root_bound_and_deterministic() -> None:
    controller = JebCollectionController(Riverhog(), Munchy())  # type: ignore[arg-type]

    first = controller.plan(recipe())
    second = controller.plan(recipe())

    assert [item.intent.transform_id for item in first] == [
        item.intent.transform_id for item in second
    ]
    assert [[root.collection_id for root in item.intent.inputs] for item in first] == [[1], [2]]


def test_tag_changes_select_work_but_do_not_change_existing_intent() -> None:
    riverhog = Riverhog()
    controller = JebCollectionController(riverhog, Munchy())  # type: ignore[arg-type]
    [first, _] = controller.plan(recipe())

    riverhog.collections[1]["tags"] = ["other"]

    assert first.intent.inputs[0].manifest_sha256 == SHA_A
    assert [item.intent.inputs[0].collection_id for item in controller.plan(recipe())] == [2]
