from __future__ import annotations

import json

from tests.fixtures.acceptance import (
    AcceptanceSystem,
)
from tests.fixtures.acceptance import (
    acceptance_system as _acceptance_system_fixture,  # noqa: F401
)
from tests.fixtures.data import (
    INVOICE_TARGET,
    SPLIT_COPY_ONE_ID,
    SPLIT_COPY_TWO_ID,
    SPLIT_FILE_RELPATH,
    TAX_DIRECTORY_TARGET,
)


def test_arc_disc_fetch_completes_a_recoverable_fetch(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(TAX_DIRECTORY_TARGET)
    acceptance_system.seed_fetch("fx-1", TAX_DIRECTORY_TARGET)
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1")
    state_dir = acceptance_system.workspace / "recovery-state" / "fx-1"

    result = acceptance_system.run_arc_disc(
        "fetch",
        "fx-1",
        "--state-dir",
        str(state_dir),
        "--device",
        "/dev/fake-sr0",
        "--json",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "done"
    assert acceptance_system.state.is_hot(TAX_DIRECTORY_TARGET) is True
    assert "Insert disc" in result.stderr


def test_arc_disc_fetch_fails_if_optical_recovery_fails(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(TAX_DIRECTORY_TARGET)
    acceptance_system.seed_fetch("fx-1", TAX_DIRECTORY_TARGET)
    acceptance_system.configure_arc_disc_fixture(
        fetch_id="fx-1",
        fail_path="tax/2022/invoice-123.pdf",
    )
    state_dir = acceptance_system.workspace / "recovery-state" / "fx-1"

    result = acceptance_system.run_arc_disc(
        "fetch",
        "fx-1",
        "--state-dir",
        str(state_dir),
        "--device",
        "/dev/fake-sr0",
    )

    assert result.returncode != 0
    assert acceptance_system.fetches.get("fx-1").state.value != "done"


def test_arc_disc_fetch_fails_if_decrypted_bytes_do_not_match_the_expected_hash(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(TAX_DIRECTORY_TARGET)
    acceptance_system.seed_fetch("fx-1", TAX_DIRECTORY_TARGET)
    acceptance_system.configure_arc_disc_fixture(
        fetch_id="fx-1",
        corrupt_path="tax/2022/invoice-123.pdf",
    )
    state_dir = acceptance_system.workspace / "recovery-state" / "fx-1"

    result = acceptance_system.run_arc_disc(
        "fetch",
        "fx-1",
        "--state-dir",
        str(state_dir),
        "--device",
        "/dev/fake-sr0",
    )

    assert result.returncode != 0
    assert acceptance_system.state.rejected_upload_codes == []
    assert acceptance_system.fetches.get("fx-1").state.value != "done"


def test_arc_disc_fetch_recovers_a_split_file_across_successive_discs(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.seed_docs_archive_with_split_invoice()
    acceptance_system.seed_pin(INVOICE_TARGET)
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1")
    state_dir = acceptance_system.workspace / "recovery-state" / "fx-1"

    result = acceptance_system.run_arc_disc(
        "fetch",
        "fx-1",
        "--state-dir",
        str(state_dir),
        "--device",
        "/dev/fake-sr0",
        "--json",
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "done"
    assert SPLIT_COPY_ONE_ID in result.stderr
    assert SPLIT_COPY_TWO_ID in result.stderr
    assert acceptance_system.state.is_hot(INVOICE_TARGET) is True
    assert (
        acceptance_system.uploaded_entry_content("fx-1", SPLIT_FILE_RELPATH)
        == acceptance_system.state.selected_files(INVOICE_TARGET)[0].content
    )


def test_arc_disc_fetch_resumes_split_file_recovery_from_state_dir(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.seed_docs_archive_with_split_invoice()
    acceptance_system.seed_pin(INVOICE_TARGET)
    acceptance_system.seed_fetch("fx-1", INVOICE_TARGET)
    state_dir = acceptance_system.workspace / "recovery-state" / "fx-1"

    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1", fail_copy_ids={SPLIT_COPY_TWO_ID})
    first = acceptance_system.run_arc_disc(
        "fetch",
        "fx-1",
        "--state-dir",
        str(state_dir),
        "--device",
        "/dev/fake-sr0",
    )

    assert first.returncode != 0
    assert (state_dir / "parts" / "e1" / "000000.part").is_file()
    assert acceptance_system.fetches.get("fx-1").state.value != "done"

    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1", fail_copy_ids={SPLIT_COPY_ONE_ID})
    second = acceptance_system.run_arc_disc(
        "fetch",
        "fx-1",
        "--state-dir",
        str(state_dir),
        "--device",
        "/dev/fake-sr0",
        "--json",
    )

    assert second.returncode == 0
    assert json.loads(second.stdout)["state"] == "done"
    assert SPLIT_COPY_ONE_ID not in second.stderr
    assert SPLIT_COPY_TWO_ID in second.stderr
    assert (
        acceptance_system.uploaded_entry_content("fx-1", SPLIT_FILE_RELPATH)
        == acceptance_system.state.selected_files(INVOICE_TARGET)[0].content
    )
