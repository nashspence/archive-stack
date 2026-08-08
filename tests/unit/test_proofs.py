from __future__ import annotations

import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from riverhog_core.proofs import (
    CommandProofStamper,
    CommandProofUpgrader,
    CommandProofVerifier,
    ProofUpgradeError,
    ProofVerifyError,
)

_COMMAND = (sys.executable, "-m", "tests.fixtures.ots_stamp_command")


def test_command_proof_verifier_accepts_matching_manifest_proof(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema":"unit/v1"}', encoding="utf-8")
    proof_path = CommandProofStamper(_COMMAND).stamp(manifest_path)

    CommandProofVerifier(_COMMAND).verify(
        manifest_bytes=manifest_path.read_bytes(),
        proof_bytes=proof_path.read_bytes(),
    )


def test_command_proof_verifier_rejects_mismatched_manifest_proof(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema":"unit/v1"}', encoding="utf-8")
    proof_path = CommandProofStamper(_COMMAND).stamp(manifest_path)

    with pytest.raises(ProofVerifyError, match="digest mismatch"):
        CommandProofVerifier(_COMMAND).verify(
            manifest_bytes=b'{"schema":"other/v1"}',
            proof_bytes=proof_path.read_bytes(),
        )


def test_command_proof_verifier_accepts_pending_blockchain_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "Calendar https://example.invalid: Pending confirmation in Bitcoin blockchain\n"
            ),
        ),
    )

    CommandProofVerifier(_COMMAND).verify(
        manifest_bytes=b"schema: unit/v1\n",
        proof_bytes=b"pending proof",
    )


def test_command_proof_verifier_accepts_transaction_awaiting_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "Calendar https://example.invalid: Timestamped by transaction "
                f"{'a' * 64}; waiting for 6 confirmations\n"
            ),
        ),
    )

    CommandProofVerifier(_COMMAND).verify(
        manifest_bytes=b"schema: unit/v1\n",
        proof_bytes=b"pending transaction proof",
    )


def test_command_proof_verifier_accepts_locally_bound_pending_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "Ignoring attestation from calendar https://example.invalid: "
                "Calendar not in whitelist\n"
            ),
        ),
    )

    CommandProofVerifier(_COMMAND).verify(
        manifest_bytes=b"schema: unit/v1\n",
        proof_bytes=b"pending proof",
    )


def test_command_proof_verifier_accepts_locally_validated_timestamp_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            args=[],
            returncode=1,
            stdout=(
                "Got 1 attestation(s) from https://example.invalid\n"
                "Calendar https://pending.invalid: "
                "Pending confirmation in Bitcoin blockchain\n"
                "Not checking Bitcoin attestation; Bitcoin disabled\n"
                "To verify manually, check that Bitcoin block 123 "
                "has merkleroot abcdef\n"
            ),
            stderr="",
        ),
    )

    CommandProofVerifier(_COMMAND).verify(
        manifest_bytes=b"schema: unit/v1\n",
        proof_bytes=b"locally validated proof",
    )


def test_command_proof_verifier_rejects_other_pending_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "Calendar https://example.invalid: "
                "Pending confirmation in Bitcoin blockchain\n"
                "deterministic proof digest mismatch\n"
            ),
        ),
    )

    with pytest.raises(ProofVerifyError, match="digest mismatch"):
        CommandProofVerifier(_COMMAND).verify(
            manifest_bytes=b"schema: unit/v1\n",
            proof_bytes=b"mismatched proof",
        )


def test_command_proof_upgrader_returns_completed_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upgrade(args: list[str], **_kwargs: object) -> CompletedProcess[str]:
        Path(args[-1]).write_bytes(b"completed proof")
        return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("riverhog_core.proofs.subprocess.run", upgrade)

    result = CommandProofUpgrader(_COMMAND).upgrade(b"pending proof")

    assert result.complete is True
    assert result.proof_bytes == b"completed proof"


def test_command_proof_upgrader_leaves_pending_proof_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda args, **_kwargs: CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="Failed! Timestamp not complete\n",
        ),
    )

    result = CommandProofUpgrader(_COMMAND).upgrade(b"pending proof")

    assert result.complete is False
    assert result.proof_bytes == b"pending proof"


def test_command_proof_upgrader_rejects_command_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "riverhog_core.proofs.subprocess.run",
        lambda args, **_kwargs: CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="Error! invalid timestamp file\n",
        ),
    )

    with pytest.raises(ProofUpgradeError, match="invalid timestamp"):
        CommandProofUpgrader(_COMMAND).upgrade(b"broken proof")
