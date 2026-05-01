from __future__ import annotations

import sys
from pathlib import Path
from re import fullmatch

import pytest

from tests.timing_profile import PROFILE

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CI_OPT_IN_REASON = "ci-opt-in: excluded from the default prod-backed harness"
CI_OPT_IN_REASON_MARKERS = {
    "requires_optical_disc_drive",
    "requires_human_operator",
    "requires_aws_s3",
    "requires_controlled_glacier_billing",
    "requires_controlled_glacier_failure",
    "requires_glacier_restore",
    "requires_opentimestamps",
    "requires_webhook_capture",
}
TODO_REASON = "scenario is specified but not automated yet"
CONTRACT_GAP_REASON = "known contract gap; see linked issue tag/comment in feature file"
TRACKER_REQUIRED_MARKERS = {"todo", "contract_gap"}


def _uses_prod_harness(item: pytest.Item) -> bool:
    return "tests/harness/test_prod_harness.py" in item.nodeid


def _has_issue_marker(item: pytest.Item) -> bool:
    return any(fullmatch(r"issue_\d+", marker.name) for marker in item.iter_markers())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        missing_tracker_markers = {
            marker.name
            for marker in item.iter_markers()
            if marker.name in TRACKER_REQUIRED_MARKERS
        }
        if missing_tracker_markers and not _has_issue_marker(item):
            names = ", ".join(sorted(missing_tracker_markers))
            raise pytest.UsageError(
                f"{item.nodeid} uses tracker-required marker(s) without issue tag: {names}"
            )

        ci_opt_in = item.get_closest_marker("ci_opt_in") is not None
        ci_opt_in_reasons = {
            marker.name
            for marker in item.iter_markers()
            if marker.name in CI_OPT_IN_REASON_MARKERS
        }
        if ci_opt_in and not ci_opt_in_reasons:
            raise pytest.UsageError(f"{item.nodeid} uses ci_opt_in without a reason marker")
        if ci_opt_in_reasons and not ci_opt_in:
            names = ", ".join(sorted(ci_opt_in_reasons))
            raise pytest.UsageError(
                f"{item.nodeid} uses opt-in reason marker(s) without ci_opt_in: {names}"
            )
        if _uses_prod_harness(item) and ci_opt_in:
            item.add_marker(pytest.mark.skip(reason=CI_OPT_IN_REASON))

        if item.get_closest_marker("todo"):
            item.add_marker(pytest.mark.skip(reason=TODO_REASON))

        if _uses_prod_harness(item) and item.get_closest_marker("contract_gap"):
            item.add_marker(pytest.mark.xfail(reason=CONTRACT_GAP_REASON, strict=True))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    PROFILE.record_test_phase(report.nodeid, report.when, report.duration)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    rendered = PROFILE.render()
    if not rendered:
        return
    terminalreporter.section("prod profile", sep="-", blue=True, bold=True)
    terminalreporter.write_line(rendered)
