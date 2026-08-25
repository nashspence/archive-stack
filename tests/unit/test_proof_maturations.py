from __future__ import annotations

import hashlib
from dataclasses import dataclass

from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveObjectRecord,
    CollectionProofMaturationRecord,
)
from riverhog_core.proofs import ProofUpgradeResult
from riverhog_core.services.proof_maturations import SqlAlchemyProofMaturationService

from tests.fixtures.crypto import FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    MemoryArchiveStore,
    archive_store_binding,
    seed_archive_copy,
)


@dataclass
class _CompleteUpgrader:
    calls: int = 0

    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult:
        self.calls += 1
        if proof_bytes.endswith(b"matured\n"):
            return ProofUpgradeResult(proof_bytes=proof_bytes, complete=True)
        return ProofUpgradeResult(
            proof_bytes=proof_bytes + b"matured\n",
            complete=True,
        )


@dataclass
class _WaitingUpgrader:
    calls: int = 0

    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult:
        self.calls += 1
        return ProofUpgradeResult(proof_bytes=proof_bytes, complete=False)


def test_matures_and_reverifies_an_archive_proof(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "state.sqlite3", {"docs/readme.txt": b"hi"})
    store = MemoryArchiveStore(archive)
    upgrader = _CompleteUpgrader()
    service = SqlAlchemyProofMaturationService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(store)}),
        proof_upgrader=upgrader,
        proof_verifier=FixtureProofVerifier(),
    )

    assert service.process_due(limit=10) == 1
    assert upgrader.calls == 1
    assert store.replaced_proofs == [archive.proof_bytes + b"matured\n"]

    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        maturation = session.get(CollectionProofMaturationRecord, (archive.collection_id, "deep"))
        proof = session.get(
            CollectionArchiveObjectRecord,
            (archive.collection_id, "deep", "proof"),
        )
        assert maturation is not None
        assert maturation.state == "matured"
        assert maturation.matured_at is not None
        assert proof is not None
        assert proof.sha256 == hashlib.sha256(store.replaced_proofs[0]).hexdigest()
        assert proof.plaintext_bytes == len(store.replaced_proofs[0])
        assert proof.revision is not None
        assert proof.revision != "fixture-proof-version"

    assert service.process_due(limit=10) == 0


def test_waits_without_replacing_an_incomplete_proof(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "state.sqlite3", {"docs/readme.txt": b"hi"})
    store = MemoryArchiveStore(archive)
    upgrader = _WaitingUpgrader()
    service = SqlAlchemyProofMaturationService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(store)}),
        proof_upgrader=upgrader,
        proof_verifier=FixtureProofVerifier(),
    )

    assert service.process_due(limit=10) == 1
    assert upgrader.calls == 1
    assert store.replaced_proofs == []

    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        maturation = session.get(CollectionProofMaturationRecord, (archive.collection_id, "deep"))
        assert maturation is not None
        assert maturation.state == "waiting"
        assert maturation.attempt_count == 1
        assert maturation.failure is None


def test_requeues_an_interrupted_maturation(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "state.sqlite3", {"docs/readme.txt": b"hi"})
    store = MemoryArchiveStore(archive)
    service = SqlAlchemyProofMaturationService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(store)}),
        proof_upgrader=_WaitingUpgrader(),
        proof_verifier=FixtureProofVerifier(),
    )
    assert service.schedule_missing() == 1

    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        maturation = session.get(CollectionProofMaturationRecord, (archive.collection_id, "deep"))
        assert maturation is not None
        maturation.state = "upgrading"

    assert service.requeue_interrupted_for_startup() == 1
    with session_scope(factory) as session:
        maturation = session.get(CollectionProofMaturationRecord, (archive.collection_id, "deep"))
        assert maturation is not None
        assert maturation.state == "pending"
        assert maturation.failure == "proof maturation interrupted before completion"
