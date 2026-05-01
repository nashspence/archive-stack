from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from arc_core.proofs import CommandProofStamper

pytestmark = [
    pytest.mark.ci_opt_in,
    pytest.mark.requires_opentimestamps,
]


def test_live_opentimestamps_command_creates_binary_proof(tmp_path: Path) -> None:
    command = tuple(shlex.split(os.environ.get("ARC_OTS_STAMP_COMMAND", "ots")))
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
