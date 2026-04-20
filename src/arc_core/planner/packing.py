from __future__ import annotations

from typing import Any

try:
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_array
except Exception as exc:  # pragma: no cover - optional dependency path
    np = None
    Bounds = None
    LinearConstraint = None
    lil_array = None
    _MILP_IMPORT_ERROR: Exception | None = exc
else:  # pragma: no cover - import success is environment-specific
    _MILP_IMPORT_ERROR = None


class PlannerDependencyError(RuntimeError):
    pass



def _require_milp() -> None:
    if _MILP_IMPORT_ERROR is not None:
        raise PlannerDependencyError(
            "MILP planner requires optional dependencies; install with `pip install .[planner]`"
        ) from _MILP_IMPORT_ERROR



def close_threshold(items: list[dict[str, Any]], fill: int, spill: int) -> int:
    return spill if any(bool(item["priority"]) for item in items) else fill



def _solve(
    items: list[dict[str, Any]],
    collections: dict[str, dict[str, int]],
    cap: int,
    fill: int,
    spill: int,
    *,
    force: bool,
) -> list[dict[str, Any]]:
    _require_milp()
    assert np is not None
    assert Bounds is not None
    assert LinearConstraint is not None
    assert lil_array is not None

    collection_ids = sorted({item["collection"] for item in items})
    item_count = len(items)
    collection_count = len(collection_ids)
    collection_index = {collection_id: idx for idx, collection_id in enumerate(collection_ids)}
    payload_bytes = np.array([item["planned_bytes"] for item in items], dtype=np.float64)
    priority = np.array([int(item["priority"]) for item in items], dtype=np.float64)
    fixed = np.array([collections[collection_id]["fixed_bytes"] for collection_id in collection_ids], dtype=np.float64)
    item_collection = np.array([collection_index[item["collection"]] for item in items], dtype=int)

    meta_pad = 2048
    fill_diff = fill - spill
    slack_index = item_count + collection_count
    deviation_index = slack_index + 1 if force else None
    variable_count = item_count + collection_count + 1 + int(force)

    row_count = 1 + item_count + collection_count + int(priority.sum()) + 2 + int(not force) + 2 * int(force)
    matrix = lil_array((row_count, variable_count), dtype=np.float64)
    lower: list[float] = []
    upper: list[float] = []
    row = 0

    matrix[row, :item_count] = payload_bytes
    matrix[row, item_count : item_count + collection_count] = fixed
    lower.append(-np.inf)
    upper.append(cap - meta_pad)
    row += 1

    for item_idx, collection_idx in enumerate(item_collection):
        matrix[row, item_idx] = 1
        matrix[row, item_count + collection_idx] = -1
        lower.append(-np.inf)
        upper.append(0)
        row += 1

    for collection_idx in range(collection_count):
        indexes = np.where(item_collection == collection_idx)[0]
        matrix[row, indexes] = -1
        matrix[row, item_count + collection_idx] = 1
        lower.append(-np.inf)
        upper.append(0)
        row += 1

    for item_idx in np.where(priority > 0)[0]:
        matrix[row, item_idx] = 1
        matrix[row, slack_index] = -1
        lower.append(-np.inf)
        upper.append(0)
        row += 1

    matrix[row, :item_count] = -priority
    matrix[row, slack_index] = 1
    lower.append(-np.inf)
    upper.append(0)
    row += 1

    matrix[row, :item_count] = 1
    lower.append(1)
    upper.append(np.inf)
    row += 1

    if force:
        assert deviation_index is not None
        matrix[row, :item_count] = payload_bytes
        matrix[row, item_count : item_count + collection_count] = fixed
        matrix[row, deviation_index] = -1
        lower.append(-np.inf)
        upper.append(fill - meta_pad)
        row += 1

        matrix[row, :item_count] = -payload_bytes
        matrix[row, item_count : item_count + collection_count] = -fixed
        matrix[row, deviation_index] = -1
        lower.append(-np.inf)
        upper.append(-(fill - meta_pad))
        row += 1
    else:
        matrix[row, :item_count] = payload_bytes
        matrix[row, item_count : item_count + collection_count] = fixed
        matrix[row, slack_index] = fill_diff
        lower.append(fill - meta_pad)
        upper.append(np.inf)
        row += 1

    integrality = np.ones(variable_count, dtype=int)
    bounds_lo = np.zeros(variable_count)
    bounds_hi = np.ones(variable_count)
    if force:
        assert deviation_index is not None
        integrality[deviation_index] = 0
        bounds_hi[deviation_index] = np.inf

    constraints = LinearConstraint(
        matrix.tocsr(), np.array(lower, dtype=np.float64), np.array(upper, dtype=np.float64)
    )
    bounds = Bounds(bounds_lo, bounds_hi)

    def run(cost: list[float], extra: tuple[tuple[Any, float, float], ...] = ()) -> dict[str, Any] | None:
        if extra:
            extra_rows = lil_array((len(extra), variable_count), dtype=np.float64)
            extra_lower: list[float] = []
            extra_upper: list[float] = []
            for idx, (vec, lo, hi) in enumerate(extra):
                extra_rows[idx] = vec
                extra_lower.append(lo)
                extra_upper.append(hi)
            local_constraints = (
                constraints,
                LinearConstraint(
                    extra_rows.tocsr(),
                    np.array(extra_lower, dtype=np.float64),
                    np.array(extra_upper, dtype=np.float64),
                ),
            )
        else:
            local_constraints = constraints
        result = milp(
            c=np.array(cost, dtype=np.float64),
            constraints=local_constraints,
            integrality=integrality,
            bounds=bounds,
            options={"mip_rel_gap": 0},
        )
        if not result.success or result.x is None:
            return None
        x = np.rint(result.x).astype(int)
        used = int(meta_pad + payload_bytes @ x[:item_count] + fixed @ x[item_count : item_count + collection_count])
        selected = [items[idx] for idx, chosen in enumerate(x[:item_count]) if chosen]
        return {"x": x, "selected": selected, "used": used}

    if force:
        assert deviation_index is not None
        cost_1 = np.zeros(variable_count)
        cost_1[deviation_index] = 1
        first = run(cost_1.tolist())
        if not first:
            return []
        deviation = int(round(first["x"][deviation_index]))
        pinned = np.zeros(variable_count)
        pinned[deviation_index] = 1
        cost_2 = np.zeros(variable_count)
        cost_2[:item_count] = -payload_bytes
        cost_2[item_count : item_count + collection_count] = -fixed
        second = run(cost_2.tolist(), ((pinned, deviation, deviation),))
        return second["selected"] if second else first["selected"]

    cost_1 = np.zeros(variable_count)
    cost_1[:item_count] = payload_bytes
    cost_1[item_count : item_count + collection_count] = fixed
    cost_1[slack_index] = fill_diff
    first = run(cost_1.tolist())
    if not first:
        return []

    slack = first["used"] - close_threshold(first["selected"], fill, spill)
    vec_1 = np.zeros(variable_count)
    vec_1[:item_count] = payload_bytes
    vec_1[item_count : item_count + collection_count] = fixed
    vec_1[slack_index] = fill_diff

    cost_2 = np.zeros(variable_count)
    cost_2[:item_count] = -payload_bytes
    cost_2[item_count : item_count + collection_count] = -fixed
    second = run(cost_2.tolist(), ((vec_1, slack, slack),))
    if not second:
        return first["selected"]

    used = second["used"]
    vec_2 = np.zeros(variable_count)
    vec_2[:item_count] = payload_bytes
    vec_2[item_count : item_count + collection_count] = fixed
    cost_3 = np.zeros(variable_count)
    cost_3[:item_count] = -priority
    third = run(cost_3.tolist(), ((vec_1, slack, slack), (vec_2, used - meta_pad, used - meta_pad)))
    return third["selected"] if third else second["selected"]



def pick_items(
    items: list[dict[str, Any]],
    collections: dict[str, dict[str, int]],
    cap: int,
    fill: int,
    spill: int,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    if not items:
        return []
    return _solve(items, collections, cap, fill, spill, force=force)
