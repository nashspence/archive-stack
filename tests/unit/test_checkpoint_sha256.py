from __future__ import annotations

import hashlib

import pytest
from riverhog_core.checkpoint_sha256 import CheckpointSHA256


@pytest.mark.parametrize("cut", [0, 1, 63, 64, 65, 511])
def test_checkpoint_sha256_matches_hashlib_across_restart(cut: int) -> None:
    content = bytes(range(256)) * 7
    digest = CheckpointSHA256(content[:cut])
    resumed = CheckpointSHA256.from_state(digest.export_state())
    resumed.update(content[cut:])

    assert resumed.hexdigest() == hashlib.sha256(content).hexdigest()


def test_checkpoint_sha256_rejects_noncanonical_private_state() -> None:
    with pytest.raises(ValueError, match="checkpoint"):
        CheckpointSHA256.from_state('{"format":"public/v1"}')
