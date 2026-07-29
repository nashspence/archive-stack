from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass

import pytest
from riverhog_core.archive_attestations import (
    CommandAttestationSigner,
    CommandAttestationVerifier,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionArchiveAttestationRecord,
    CollectionArchiveObjectRecord,
    CollectionProofMaturationRecord,
)
from riverhog_core.proofs import ProofUpgradeResult
from riverhog_core.services.archive_attestations import SqlAlchemyArchiveAttestationService

from tests.fixtures.crypto import FixtureProofStamper, FixtureProofVerifier
from tests.unit.archive_object_fixtures import (
    MemoryArchiveStore,
    as_archive_store,
    seed_archive_copy,
)


@pytest.mark.skipif(shutil.which("minisign") is None, reason="minisign is not installed")
def test_command_signer_uses_a_private_minisign_key(tmp_path) -> None:
    secret_key = tmp_path / "minisign.key"
    public_key = tmp_path / "minisign.pub"
    subprocess.run(
        [
            "minisign",
            "-G",
            "-W",
            "-s",
            str(secret_key),
            "-p",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    checksums = b"0" * 64 + b"  objects/data-000000.age\n"

    signature = CommandAttestationSigner(secret_key).sign(checksums)

    CommandAttestationVerifier(public_key).verify(
        checksums=checksums,
        signature=signature,
    )


@dataclass
class _Signer:
    def sign(self, checksums: bytes) -> bytes:
        return b"fixture-minisign:" + hashlib.sha256(checksums).hexdigest().encode()


@dataclass
class _SignatureVerifier:
    def verify(self, *, checksums: bytes, signature: bytes) -> None:
        assert signature == _Signer().sign(checksums)


@dataclass
class _CompleteUpgrader:
    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult:
        if proof_bytes.endswith(b"matured\n"):
            return ProofUpgradeResult(proof_bytes=proof_bytes, complete=True)
        return ProofUpgradeResult(proof_bytes=proof_bytes + b"matured\n", complete=True)


@dataclass
class _WaitingUpgrader:
    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult:
        return ProofUpgradeResult(proof_bytes=proof_bytes, complete=False)


def test_publishes_signs_and_matures_exact_archive_ciphertext_inventory(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "state.sqlite3", {"docs/readme.txt": b"hi"})
    _mark_manifest_proof_matured(config.database_url, archive.collection_id)
    store = MemoryArchiveStore(archive)
    service = SqlAlchemyArchiveAttestationService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        signer=_Signer(),
        signature_verifier=_SignatureVerifier(),
        proof_stamper=FixtureProofStamper(),
        proof_upgrader=_CompleteUpgrader(),
        proof_verifier=FixtureProofVerifier(),
    )

    assert service.process_due(limit=10) == 1
    checksums = store.attestation_artifacts["checksums"]
    assert checksums.decode().splitlines() == [
        f"{archive.manifest_sha256}  manifest.yml.age",
        f"{archive.proof_sha256}  manifest.yml.ots.age",
        f"{archive.data_objects[0].sha256}  objects/data-000000.age",
    ]
    assert store.attestation_artifacts["signature"] == _Signer().sign(checksums)

    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(
            CollectionArchiveAttestationRecord,
            (archive.collection_id, "deep"),
        )
        assert record is not None
        assert record.state == "waiting"
        record.next_attempt_at = "2026-01-01T00:00:00.000000Z"

    assert service.process_due(limit=10) == 1
    with session_scope(factory) as session:
        record = session.get(
            CollectionArchiveAttestationRecord,
            (archive.collection_id, "deep"),
        )
        assert record is not None
        assert record.state == "matured"
        assert record.matured_at is not None
        objects = session.query(CollectionArchiveObjectRecord).filter_by(
            collection_id=archive.collection_id,
            store="deep",
        )
        assert {current.object_id for current in objects} >= {
            "checksums",
            "signature",
            "signature-proof",
        }


def test_waits_without_replacing_an_incomplete_attestation_proof(tmp_path) -> None:
    config, archive = seed_archive_copy(tmp_path / "state.sqlite3", {"docs/readme.txt": b"hi"})
    _mark_manifest_proof_matured(config.database_url, archive.collection_id)
    store = MemoryArchiveStore(archive)
    service = SqlAlchemyArchiveAttestationService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(store)}),
        signer=_Signer(),
        signature_verifier=_SignatureVerifier(),
        proof_stamper=FixtureProofStamper(),
        proof_upgrader=_WaitingUpgrader(),
        proof_verifier=FixtureProofVerifier(),
    )
    assert service.process_due(limit=10) == 1
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(
            CollectionArchiveAttestationRecord,
            (archive.collection_id, "deep"),
        )
        assert record is not None
        record.next_attempt_at = "2026-01-01T00:00:00.000000Z"

    initial_proof = store.attestation_artifacts["signature-proof"]
    assert service.process_due(limit=10) == 1
    assert store.attestation_artifacts["signature-proof"] == initial_proof
    with session_scope(factory) as session:
        record = session.get(
            CollectionArchiveAttestationRecord,
            (archive.collection_id, "deep"),
        )
        assert record is not None
        assert record.state == "waiting"
        assert record.failure is None


@pytest.mark.parametrize(
    ("claimed_state", "resumed_state"),
    (("publishing", "pending"), ("upgrading", "waiting")),
)
def test_startup_resumes_a_claimed_archive_attestation(
    tmp_path,
    claimed_state: str,
    resumed_state: str,
) -> None:
    config, archive = seed_archive_copy(tmp_path / "state.sqlite3", {"docs/readme.txt": b"hi"})
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionArchiveAttestationRecord(
                collection_id=archive.collection_id,
                store="deep",
                state=claimed_state,
                attempt_count=1,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
            )
        )
    service = SqlAlchemyArchiveAttestationService(
        config,
        ArchiveStoreRegistry({"deep": as_archive_store(MemoryArchiveStore(archive))}),
        signer=_Signer(),
        signature_verifier=_SignatureVerifier(),
        proof_stamper=FixtureProofStamper(),
        proof_upgrader=_WaitingUpgrader(),
        proof_verifier=FixtureProofVerifier(),
    )

    assert service.requeue_interrupted_for_startup() == 1
    with session_scope(factory) as session:
        record = session.get(
            CollectionArchiveAttestationRecord,
            (archive.collection_id, "deep"),
        )
        assert record is not None
        assert record.state == resumed_state
        assert record.next_attempt_at is not None


def _mark_manifest_proof_matured(database_url: str, collection_id: int) -> None:
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionProofMaturationRecord(
                collection_id=collection_id,
                store="deep",
                state="matured",
                attempt_count=1,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
                matured_at="2026-01-01T00:00:00.000000Z",
            )
        )
