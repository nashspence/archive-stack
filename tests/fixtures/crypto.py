from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FixtureProofStamper:
    def stamp(self, manifest_path: Path) -> Path:
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        proof_path = manifest_path.with_name(f"{manifest_path.name}.ots")
        proof_path.write_text(
            "\n".join(
                [
                    "OpenTimestamps test proof v1",
                    f"file: {manifest_path.name}",
                    f"sha256: {digest}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return proof_path


@dataclass(frozen=True, slots=True)
class FixtureProofVerifier:
    def verify(self, *, manifest_bytes: bytes, proof_bytes: bytes) -> None:
        digest = hashlib.sha256(manifest_bytes).hexdigest().encode()
        if digest not in proof_bytes:
            raise ValueError("fixture proof does not match manifest")
