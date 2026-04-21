from __future__ import annotations

import json

from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import TAX_DIRECTORY_TARGET


def test_arc_disc_fetch_completes_a_recoverable_fetch(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(TAX_DIRECTORY_TARGET)
    acceptance_system.seed_fetch("fx-1", TAX_DIRECTORY_TARGET)
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1")

    result = acceptance_system.run_arc_disc("fetch", "fx-1", "--device", "/dev/fake-sr0", "--json")

    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "done"
    assert acceptance_system.state.is_hot(TAX_DIRECTORY_TARGET) is True


def test_arc_disc_fetch_fails_if_optical_recovery_fails(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(TAX_DIRECTORY_TARGET)
    acceptance_system.seed_fetch("fx-1", TAX_DIRECTORY_TARGET)
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1", fail_path="tax/2022/invoice-123.pdf")

    result = acceptance_system.run_arc_disc("fetch", "fx-1", "--device", "/dev/fake-sr0")

    assert result.returncode != 0
    assert acceptance_system.fetches.get("fx-1").state.value != "done"


def test_arc_disc_fetch_fails_if_decrypted_bytes_do_not_match_the_expected_hash(
    acceptance_system: AcceptanceSystem,
) -> None:
    acceptance_system.seed_docs_archive()
    acceptance_system.seed_pin(TAX_DIRECTORY_TARGET)
    acceptance_system.seed_fetch("fx-1", TAX_DIRECTORY_TARGET)
    acceptance_system.configure_arc_disc_fixture(fetch_id="fx-1", corrupt_path="tax/2022/invoice-123.pdf")

    result = acceptance_system.run_arc_disc("fetch", "fx-1", "--device", "/dev/fake-sr0")

    assert result.returncode != 0
    assert "hash_mismatch" in acceptance_system.state.rejected_upload_codes
    assert acceptance_system.fetches.get("fx-1").state.value != "done"
