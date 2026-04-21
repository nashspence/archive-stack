from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc_core.planner import packing


def _item(
    item_id: str,
    collection: str,
    planned_bytes: int,
    *,
    priority: bool = False,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "collection": collection,
        "planned_bytes": planned_bytes,
        "priority": priority,
    }


def _selected_ids(selected: list[dict[str, object]]) -> list[str]:
    return [str(item["item_id"]) for item in selected]


def _result(*, x: list[float] | None = None, success: bool) -> SimpleNamespace:
    payload = None if x is None else packing.np.array(x, dtype=float)
    return SimpleNamespace(success=success, x=payload)


def test_require_milp_raises_a_helpful_error_when_optional_dependencies_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = RuntimeError("planner extras missing")
    monkeypatch.setattr(packing, "_MILP_IMPORT_ERROR", missing)

    with pytest.raises(
        packing.PlannerDependencyError,
        match="install with `pip install .\\[planner\\]`",
    ) as exc_info:
        packing._require_milp()

    assert exc_info.value.__cause__ is missing


def test_pick_items_returns_empty_when_no_items_are_available() -> None:
    assert packing.pick_items([], {}, cap=10_000, fill=8_000) == []


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_prefers_priority_items_when_used_bytes_are_equal() -> None:
    items = [
        _item("priority", "c1", 3_000, priority=True),
        _item("regular", "c2", 3_000),
    ]

    selected = packing.pick_items(
        items,
        {"c1": {"fixed_bytes": 0}, "c2": {"fixed_bytes": 0}},
        cap=7_000,
        fill=5_048,
    )

    assert _selected_ids(selected) == ["priority"]


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_counts_collection_fixed_bytes_once_per_collection() -> None:
    items = [
        _item("a", "shared", 1_700),
        _item("b", "shared", 1_700),
        _item("c", "other", 1_700),
    ]

    selected = packing.pick_items(
        items,
        {"shared": {"fixed_bytes": 1_200}, "other": {"fixed_bytes": 1_200}},
        cap=7_000,
        fill=6_500,
    )

    assert _selected_ids(selected) == ["a", "b"]


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_returns_empty_when_min_fill_cannot_be_met() -> None:
    selected = packing.pick_items(
        [_item("a", "c1", 1_000)],
        {"c1": {"fixed_bytes": 100}},
        cap=5_000,
        fill=4_500,
    )

    assert selected == []


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_force_mode_prefers_more_used_bytes_for_the_same_fill_distance() -> None:
    items = [
        _item("lower", "c1", 3_452),
        _item("higher", "c2", 4_452),
    ]

    selected = packing.pick_items(
        items,
        {"c1": {"fixed_bytes": 0}, "c2": {"fixed_bytes": 0}},
        cap=7_000,
        fill=6_000,
        force=True,
    )

    assert _selected_ids(selected) == ["higher"]


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_force_mode_uses_priority_as_a_final_tiebreaker() -> None:
    items = [
        _item("priority", "c1", 3_452, priority=True),
        _item("regular", "c2", 3_452),
    ]

    selected = packing.pick_items(
        items,
        {"c1": {"fixed_bytes": 0}, "c2": {"fixed_bytes": 0}},
        cap=6_000,
        fill=6_000,
        force=True,
    )

    assert _selected_ids(selected) == ["priority"]


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_force_mode_returns_empty_when_nothing_fits() -> None:
    selected = packing.pick_items(
        [_item("a", "c1", 4_000)],
        {"c1": {"fixed_bytes": 2_000}},
        cap=5_000,
        fill=4_500,
        force=True,
    )

    assert selected == []


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_returns_the_first_solution_when_priority_tiebreak_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    results = iter(
        [
            _result(x=[1, 1], success=True),
            _result(success=False),
        ]
    )

    def fake_milp(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs["constraints"])
        return next(results)

    monkeypatch.setattr(packing, "milp", fake_milp)

    selected = packing.pick_items(
        [_item("a", "c1", 100, priority=True)],
        {"c1": {"fixed_bytes": 0}},
        cap=5_000,
        fill=3_000,
    )

    assert _selected_ids(selected) == ["a"]
    assert len(calls) == 2
    assert isinstance(calls[0], packing.LinearConstraint)
    assert isinstance(calls[1], tuple)


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_force_mode_returns_the_first_solution_when_second_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        [
            _result(x=[1, 1, 500], success=True),
            _result(success=False),
        ]
    )

    monkeypatch.setattr(packing, "milp", lambda **_: next(results))

    selected = packing.pick_items(
        [_item("a", "c1", 100)],
        {"c1": {"fixed_bytes": 0}},
        cap=5_000,
        fill=3_000,
        force=True,
    )

    assert _selected_ids(selected) == ["a"]


@pytest.mark.skipif(packing.np is None, reason="planner extras are not installed")
def test_pick_items_force_mode_returns_the_second_solution_when_priority_tiebreak_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        [
            _result(x=[1, 0, 1, 0, 500], success=True),
            _result(x=[0, 1, 0, 1, 500], success=True),
            _result(success=False),
        ]
    )

    monkeypatch.setattr(packing, "milp", lambda **_: next(results))

    selected = packing.pick_items(
        [
            _item("first", "c1", 100, priority=True),
            _item("second", "c2", 100),
        ],
        {"c1": {"fixed_bytes": 0}, "c2": {"fixed_bytes": 0}},
        cap=5_000,
        fill=3_000,
        force=True,
    )

    assert _selected_ids(selected) == ["second"]
