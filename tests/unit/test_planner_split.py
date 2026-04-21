from __future__ import annotations

import pytest

from arc_core.planner.split import leaves, split_collection, tree_plan


def _piece(piece_index: int, piece_count: int, estimated_on_disc_bytes: int) -> dict[str, int]:
    return {
        "piece_index": piece_index,
        "piece_count": piece_count,
        "estimated_on_disc_bytes": estimated_on_disc_bytes,
    }


def test_tree_plan_splits_oversized_directories_and_reuses_free_space() -> None:
    planned = tree_plan(
        {"": ["large", "medium", "small"]},
        {"": 15, "large": 7, "medium": 5, "small": 3},
        cap=10,
    )

    assert planned == [
        {
            "pieces": [],
            "bytes": 10,
            "reason": "split",
            "nodes": [("large", "split"), ("small", "split")],
        },
        {"pieces": [], "bytes": 5, "reason": "split", "nodes": [("medium", "split")]},
    ]


def test_leaves_walks_nested_children_in_logical_order() -> None:
    children = {
        "root": ["a", "dir"],
        "dir": ["b", "c"],
    }

    assert list(leaves("root", children)) == ["a", "b", "c"]


def test_split_collection_marks_directory_splits_and_volume_splits() -> None:
    files = [
        {
            "relpath": "/docs/a.txt",
            "pieces": [_piece(0, 1, 6)],
        },
        {
            "relpath": "/docs/b.bin",
            "pieces": [_piece(0, 2, 4), _piece(1, 2, 4)],
        },
    ]

    planned = split_collection(
        files=files,
        children={"": ["/docs"], "/docs": ["/docs/a.txt", "/docs/b.bin"]},
        directories=["", "/docs"],
        cap=8,
    )

    assert planned == [
        {"pieces": [_piece(0, 2, 4), _piece(1, 2, 4)], "bytes": 8, "reason": "volume"},
        {"pieces": [_piece(0, 1, 6)], "bytes": 6, "reason": "split"},
    ]


def test_split_collection_keeps_unsplit_content_as_a_directory_plan_when_it_fits() -> None:
    planned = split_collection(
        files=[{"relpath": "/docs/a.txt", "pieces": [_piece(0, 1, 6)]}],
        children={"": ["/docs/a.txt"]},
        directories=[""],
        cap=8,
    )

    assert planned == [{"pieces": [_piece(0, 1, 6)], "bytes": 6, "reason": "dir"}]


def test_split_collection_upgrades_a_directory_plan_to_split_when_a_split_node_is_mixed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "arc_core.planner.split.tree_plan",
        lambda *_args, **_kwargs: [
            {
                "pieces": [],
                "bytes": 0,
                "reason": "dir",
                "nodes": [("/docs/a.txt", "split")],
            }
        ],
    )

    planned = split_collection(
        files=[{"relpath": "/docs/a.txt", "pieces": [_piece(0, 1, 6)]}],
        children={"": ["/docs/a.txt"]},
        directories=[""],
        cap=8,
    )

    assert planned == [{"pieces": [_piece(0, 1, 6)], "bytes": 6, "reason": "split"}]
