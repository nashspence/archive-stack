from __future__ import annotations

from collections.abc import Mapping

STOVE0_OPERATION_ERROR_CODES: Mapping[str, frozenset[str]] = {
    "get_recipe": frozenset({"not_found"}),
    "create_work": frozenset({"conflict", "not_found"}),
    "get_work": frozenset({"not_found"}),
    "inspect_work_coordination": frozenset({"not_found"}),
    "get_artifact_selection": frozenset({"not_found"}),
    "step_work": frozenset({"conflict", "not_found"}),
    "retry_work": frozenset({"conflict", "not_found"}),
    "cancel_work": frozenset({"conflict", "not_found"}),
    "preview_workflow": frozenset({"not_found"}),
    "create_evaluation": frozenset({"conflict"}),
    "get_evaluation": frozenset({"conflict", "not_found"}),
    "step_evaluation": frozenset({"conflict", "not_found"}),
    "cancel_evaluation": frozenset({"conflict", "not_found"}),
    "retry_evaluation_variant": frozenset({"conflict", "not_found"}),
    "review_evaluation_variant": frozenset({"conflict", "not_found"}),
    "run_scheduler": frozenset({"conflict"}),
}
