from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import pytest_collection_modifyitems


class _CollectedItem:
    def __init__(self, *marker_names: str, nodeid: str = "tests/harness/test_spec_harness.py::x"):
        self.nodeid = nodeid
        self._markers = [SimpleNamespace(name=name) for name in marker_names]
        self.added_markers: list[object] = []

    def iter_markers(self):
        return iter(self._markers)

    def get_closest_marker(self, name: str):
        return next((marker for marker in self._markers if marker.name == name), None)

    def add_marker(self, marker: object) -> None:
        self.added_markers.append(marker)


@pytest.mark.parametrize("marker_name", ["todo", "contract_gap"])
def test_tracker_required_readiness_markers_require_issue_tag(marker_name: str) -> None:
    item = _CollectedItem(marker_name)

    with pytest.raises(pytest.UsageError, match="without issue tag"):
        pytest_collection_modifyitems([item])  # type: ignore[list-item]


@pytest.mark.parametrize("marker_name", ["todo", "contract_gap"])
def test_tracker_required_readiness_markers_accept_issue_tag(marker_name: str) -> None:
    item = _CollectedItem(marker_name, "issue_186")

    pytest_collection_modifyitems([item])  # type: ignore[list-item]
