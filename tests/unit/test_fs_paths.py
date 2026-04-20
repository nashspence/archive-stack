from __future__ import annotations

from pathlib import Path

import pytest

from arc_core.fs_paths import normalize_relpath, normalize_root_node_name, path_parents



def test_normalize_relpath_strips_and_normalizes() -> None:
    assert normalize_relpath(' a\\b/c ') == 'a/b/c'



def test_normalize_relpath_rejects_escape() -> None:
    with pytest.raises(ValueError):
        normalize_relpath('../x')



def test_normalize_root_node_name_rejects_nested() -> None:
    with pytest.raises(ValueError):
        normalize_root_node_name('a/b')



def test_path_parents_lists_intermediate_dirs() -> None:
    assert path_parents('a/b/c.txt') == ['a', 'a/b']
