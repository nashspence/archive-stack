from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from riverhog_provenance import (
    ProvenanceObserverError,
    load_or_create_installation_id,
    user_installation_id,
)


def test_installation_identity_is_opaque_persisted_and_installation_scoped(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first" / "provenance-installation-id"
    second_path = tmp_path / "second" / "provenance-installation-id"

    first = load_or_create_installation_id(first_path)
    repeated = load_or_create_installation_id(first_path)
    second = load_or_create_installation_id(second_path)

    assert first == repeated
    assert first != second
    assert first == f"urn:uuid:{uuid.UUID(first.removeprefix('urn:uuid:'))}"
    assert first_path.read_text(encoding="ascii") == first + "\n"


def test_installation_identity_rejects_noncanonical_persisted_state(tmp_path: Path) -> None:
    path = tmp_path / "provenance-installation-id"
    path.write_text("host.example.test\n", encoding="ascii")

    with pytest.raises(ProvenanceObserverError, match="not a UUID URN"):
        load_or_create_installation_id(path)


def test_user_installation_identity_uses_shared_state_convention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIVERHOG_PROVENANCE_STATE_HOME", str(tmp_path))

    identity = user_installation_id("riverhog-client")

    assert (tmp_path / "riverhog-client" / "provenance-installation-id").read_text(
        encoding="ascii"
    ) == identity + "\n"
