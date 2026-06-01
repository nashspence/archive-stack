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


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    PROFILE.record_test_phase(report.nodeid, report.when, report.duration)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    rendered = PROFILE.render()
    if not rendered:
        return
    terminalreporter.section("bdd profile", sep="-", blue=True, bold=True)
    terminalreporter.write_line(rendered)
