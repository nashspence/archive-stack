from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.timing_profile import PROFILE

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


XFAIL_REASONS = {
    "xfail_contract": ("spec backing exists, but prod is not implemented to the contract yet"),
    "xfail_not_backed": ("Gherkin contract exists, but prod backing is not implemented yet"),
}
STRICT_XFAIL_MARKERS = {"xfail_contract", "xfail_not_backed"}


def _uses_spec_harness(item: pytest.Item) -> bool:
    return "tests/harness/test_spec_harness.py" in item.nodeid


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        xfail_markers = {
            marker.name for marker in item.iter_markers() if marker.name in XFAIL_REASONS
        }
        if _uses_spec_harness(item):
            xfail_markers.discard("xfail_contract")
        if len(xfail_markers) > 1:
            names = ", ".join(sorted(xfail_markers))
            raise pytest.UsageError(
                f"{item.nodeid} cannot use more than one xfail readiness marker: {names}"
            )
        if xfail_markers:
            marker_name = next(iter(xfail_markers))
            item.add_marker(
                pytest.mark.xfail(
                    reason=XFAIL_REASONS[marker_name],
                    strict=marker_name in STRICT_XFAIL_MARKERS,
                )
            )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    PROFILE.record_test_phase(report.nodeid, report.when, report.duration)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    rendered = PROFILE.render()
    if not rendered:
        return
    terminalreporter.section("prod profile", sep="-", blue=True, bold=True)
    terminalreporter.write_line(rendered)
