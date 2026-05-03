from __future__ import annotations

import pytest

from arc_core.operator_statecharts import (
    OperatorDecision,
    OperatorView,
    StatechartCatalogError,
    load_default_statechart_catalog,
)


def test_statechart_catalog_validates_default_contract_against_schema() -> None:
    catalog = load_default_statechart_catalog(validate_schema=True)

    assert "arc.home" in catalog.statecharts
    assert catalog.require_statechart("arc.home")["initial"] == "scan_attention"


def test_statechart_catalog_resolves_states_views_transitions_and_handoffs() -> None:
    catalog = load_default_statechart_catalog(validate_schema=True)

    assert catalog.view_for("arc.hot_storage", "pin_waiting_for_disc") == (
        "pin_waiting_for_disc"
    )
    assert catalog.transition_targets("arc.hot_storage", "choose_operation") == (
        "api_unreachable",
        "search_header",
        "get_starting",
        "pin_requested",
        "pins_list",
        "fetch_detail_pending",
        "release_done",
    )
    handoff = catalog.handoffs_from("arc.hot_storage", "pin_waiting_for_disc")[0]
    assert handoff.target_statechart == "arc_disc.guided"


def test_statechart_catalog_builds_operator_decisions_and_views() -> None:
    catalog = load_default_statechart_catalog(validate_schema=True)

    decision = catalog.decision("arc.home", "no_attention")
    view = catalog.operator_view("arc.home", "no_attention", text="No attention needed.")

    assert decision == OperatorDecision(statechart="arc.home", state="no_attention")
    assert view == OperatorView(
        statechart="arc.home",
        state="no_attention",
        copy_ref="arc_home_no_attention",
        text="No attention needed.",
    )


def test_statechart_catalog_rejects_missing_states_and_viewless_views() -> None:
    catalog = load_default_statechart_catalog(validate_schema=True)

    with pytest.raises(StatechartCatalogError):
        catalog.require_state("arc.home", "missing")

    with pytest.raises(StatechartCatalogError):
        catalog.operator_view("operator.notifications", "no_labeling_notification", text="")
