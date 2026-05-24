from __future__ import annotations

import sys
from pathlib import Path

import pytest

from riverhog_core.proofs import CommandProofStamper, CommandProofVerifier, ProofVerifyError

_COMMAND = (sys.executable, "-m", "tests.fixtures.ots_stamp_command")


def test_command_proof_verifier_accepts_matching_manifest_proof(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    manifest_path.write_text("schema: unit/v1\n", encoding="utf-8")
    proof_path = CommandProofStamper(_COMMAND).stamp(manifest_path)

    CommandProofVerifier(_COMMAND).verify(
        manifest_bytes=manifest_path.read_bytes(),
        proof_bytes=proof_path.read_bytes(),
    )


def test_command_proof_verifier_rejects_mismatched_manifest_proof(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.yml"
    manifest_path.write_text("schema: unit/v1\n", encoding="utf-8")
    proof_path = CommandProofStamper(_COMMAND).stamp(manifest_path)

    with pytest.raises(ProofVerifyError, match="digest mismatch"):
        CommandProofVerifier(_COMMAND).verify(
            manifest_bytes=b"schema: other/v1\n",
            proof_bytes=proof_path.read_bytes(),
        )
