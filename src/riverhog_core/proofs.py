from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ProofStampError(RuntimeError):
    pass


class ProofVerifyError(RuntimeError):
    pass


class ProofStamper(Protocol):
    def stamp(self, manifest_path: Path) -> Path: ...


class ProofVerifier(Protocol):
    def verify(self, *, manifest_bytes: bytes, proof_bytes: bytes) -> None: ...


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


@dataclass(frozen=True)
class CommandProofVerifier:
    command: Sequence[str] = ("ots",)

    def verify(self, *, manifest_bytes: bytes, proof_bytes: bytes) -> None:
        if not self.command:
            raise ProofVerifyError("proof verify command is empty")
        with tempfile.TemporaryDirectory(prefix="riverhog-ots-verify-") as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.yml"
            proof_path = root / "manifest.yml.ots"
            manifest_path.write_bytes(manifest_bytes)
            proof_path.write_bytes(proof_bytes)
            proc = subprocess.run(
                [*self.command, "verify", str(proof_path), "-f", str(manifest_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        if proc.returncode != 0:
            raise ProofVerifyError(proc.stderr or proc.stdout or "proof verification failed")
