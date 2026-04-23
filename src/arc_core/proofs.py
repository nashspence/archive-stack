from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProofStampError(RuntimeError):
    pass


class ProofStamper(Protocol):
    def stamp(self, manifest_path: Path) -> Path: ...


@dataclass(frozen=True)
class StubProofStamper:
    def stamp(self, manifest_path: Path) -> Path:
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        proof_path = manifest_path.with_name(f"{manifest_path.name}.ots")
        proof_path.write_text(
            "\n".join(
                [
                    "OpenTimestamps stub proof v1",
                    f"file: {manifest_path.name}",
                    f"sha256: {digest}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return proof_path


@dataclass(frozen=True)
class CommandProofStamper:
    command: list[str]

    def stamp(self, manifest_path: Path) -> Path:
        proc = subprocess.run(
            [*self.command, "stamp", str(manifest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ProofStampError(proc.stderr or proc.stdout or "proof stamping failed")
        proof_path = manifest_path.with_name(f"{manifest_path.name}.ots")
        if not proof_path.exists():
            raise ProofStampError("proof stamp command did not create .ots file")
        return proof_path
