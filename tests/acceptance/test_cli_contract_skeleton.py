from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(reason="Acceptance skeleton only; implement adapters and persistence first")


def test_arc_pin_json_matches_api() -> None:
    pass


def test_arc_release_json_matches_api() -> None:
    pass


def test_arc_disc_fetch_completes_fetch() -> None:
    pass
