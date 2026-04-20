from __future__ import annotations

import pytest


pytestmark = pytest.mark.skip(reason="Acceptance skeleton only; implement adapters and persistence first")


def test_close_collection_materializes_hot() -> None:
    pass


def test_pin_collection_already_hot_no_fetch() -> None:
    pass


def test_release_missing_pin_is_noop() -> None:
    pass


def test_register_copy_increases_archived_coverage() -> None:
    pass


def test_pin_cold_archived_file_creates_fetch() -> None:
    pass


def test_complete_fetch_makes_target_hot() -> None:
    pass
