from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

FIXTURE_AGE_PREFIX = b"fixture-age-plugin-batchpass/v1\n"


@dataclass(frozen=True, slots=True)
class FixtureRecoveryPayloadCodec:
    @property
    def metadata(self) -> Mapping[str, object]:
        return {"alg": "age-plugin-batchpass/v1", "fixture": True}

    def encrypt(self, content: bytes) -> bytes:
        return FIXTURE_AGE_PREFIX + base64.b64encode(content) + b"\n"

    def decrypt(self, content: bytes) -> bytes:
        if not content.startswith(FIXTURE_AGE_PREFIX):
            raise ValueError("recovery payload is missing the expected prefix")
        return base64.b64decode(content[len(FIXTURE_AGE_PREFIX) :].strip(), validate=True)


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
