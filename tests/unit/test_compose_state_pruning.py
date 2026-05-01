from __future__ import annotations

from pathlib import Path

from scripts.prune_compose_state import (
    is_generated_prod_harness_state_name,
    select_generated_prod_harness_state_roots,
)


def test_generated_prod_harness_state_names_are_selected_conservatively() -> None:
    assert is_generated_prod_harness_state_name("archive-stack-test-codespace-167907")
    assert is_generated_prod_harness_state_name("archive-stack-test-user-name-167907")

    assert not is_generated_prod_harness_state_name("acceptance")
    assert not is_generated_prod_harness_state_name("archive-stack-shared")
    assert not is_generated_prod_harness_state_name("archive-stack-test-codespace")
    assert not is_generated_prod_harness_state_name("archive-stack-test-codespace-debug")
    assert not is_generated_prod_harness_state_name("archive-stack-test-codespace-")


def test_generated_prod_harness_state_roots_are_selected_without_shared_state(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / ".compose"
    selected_names = [
        "archive-stack-test-codespace-167907",
        "archive-stack-test-user-name-167908",
    ]
    preserved_names = [
        "acceptance",
        "archive-stack-shared",
        "archive-stack-test-codespace",
        "archive-stack-test-codespace-debug",
    ]
    for name in selected_names + preserved_names:
        (state_root / name).mkdir(parents=True)

    selected = select_generated_prod_harness_state_roots(state_root)

    assert [path.name for path in selected] == selected_names
