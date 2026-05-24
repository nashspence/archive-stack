from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from riverhog_core.proofs import CommandProofStamper, CommandProofVerifier

pytestmark = [
    pytest.mark.ci_opt_in,
    pytest.mark.requires_opentimestamps,
]


def test_live_opentimestamps_command_creates_binary_proof(tmp_path: Path) -> None:
    command = tuple(shlex.split(os.environ.get("RIVERHOG_OTS_STAMP_COMMAND", "ots")))
    if shutil.which(command[0]) is None:
        pytest.skip("ots command is not available")
    manifest_path = tmp_path / "manifest.yml"
    manifest_path.write_text("schema: ci-opt-in/v1\n", encoding="utf-8")

    proof_path = CommandProofStamper(command).stamp(manifest_path)

    proof_bytes = proof_path.read_bytes()
    assert proof_path == tmp_path / "manifest.yml.ots"
    assert proof_bytes
    assert not proof_bytes.startswith(b"OpenTimestamps stub proof v1")
    assert not proof_bytes.startswith(b"OpenTimestamps test proof v1")


def test_live_opentimestamps_command_verifies_binary_proof(tmp_path: Path) -> None:
    stamp_command = tuple(shlex.split(os.environ.get("RIVERHOG_OTS_STAMP_COMMAND", "ots")))
    verify_command = tuple(shlex.split(os.environ.get("RIVERHOG_OTS_VERIFY_COMMAND", "ots")))
    if shutil.which(stamp_command[0]) is None:
        pytest.skip("ots stamp command is not available")
    if shutil.which(verify_command[0]) is None:
        pytest.skip("ots verify command is not available")
    manifest_path = tmp_path / "manifest.yml"
    manifest_path.write_text("schema: ci-opt-in/v1\n", encoding="utf-8")
    proof_path = CommandProofStamper(stamp_command).stamp(manifest_path)

    CommandProofVerifier(verify_command).verify(
        manifest_bytes=manifest_path.read_bytes(),
        proof_bytes=proof_path.read_bytes(),
    )
