from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(reason="Acceptance skeleton only; implement adapters and persistence first")


def test_arc_cli_feature() -> None:
    """Covers: tests/acceptance/features/cli.arc.feature"""



def test_arc_disc_cli_feature() -> None:
    """Covers: tests/acceptance/features/cli.arc_disc.feature"""
