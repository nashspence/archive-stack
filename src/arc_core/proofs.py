from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProofStampError(RuntimeError):
    pass


class ProofStamper(Protocol):
    def stamp(self, manifest_path: Path) -> Path: ...


@dataclass(frozen=True)
class CommandProofStamper:
    command: Sequence[str] = ("ots",)

    def stamp(self, manifest_path: Path) -> Path:
        if not self.command:
            raise ProofStampError("proof stamp command is empty")
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
