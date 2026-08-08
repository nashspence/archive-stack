from __future__ import annotations

import re
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


class ProofUpgradeError(RuntimeError):
    pass


class ProofStamper(Protocol):
    def stamp(self, manifest_path: Path) -> Path: ...


class ProofVerifier(Protocol):
    def verify(self, *, manifest_bytes: bytes, proof_bytes: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class ProofUpgradeResult:
    proof_bytes: bytes
    complete: bool


class ProofUpgrader(Protocol):
    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult: ...


def _is_nonfatal_timestamp_status(stdout: str, stderr: str) -> bool:
    lines = [
        line.strip() for output in (stdout, stderr) for line in output.splitlines() if line.strip()
    ]

    def pending(line: str) -> bool:
        return line.startswith("Calendar ") and line.endswith(
            ": Pending confirmation in Bitcoin blockchain"
        )

    def awaiting_confirmations(line: str) -> bool:
        return bool(
            re.fullmatch(
                r"Calendar \S+: Timestamped by transaction [0-9a-f]{64}; "
                r"waiting for [1-9][0-9]* confirmations",
                line,
            )
        )

    def attestation(line: str) -> bool:
        return line.startswith("Got ") and " attestation(s) from " in line

    def ignored_calendar(line: str) -> bool:
        return line.startswith("Ignoring attestation from calendar ") and line.endswith(
            ": Calendar not in whitelist"
        )

    def bitcoin_disabled(line: str) -> bool:
        return line == "Not checking Bitcoin attestation; Bitcoin disabled"

    def manual_check(line: str) -> bool:
        return (
            line.startswith("To verify manually, check that Bitcoin block ")
            and " has merkleroot " in line
        )

    allowed = all(
        pending(line)
        or awaiting_confirmations(line)
        or attestation(line)
        or ignored_calendar(line)
        or bitcoin_disabled(line)
        or manual_check(line)
        for line in lines
    )
    has_deferred_status = any(
        pending(line)
        or awaiting_confirmations(line)
        or ignored_calendar(line)
        or bitcoin_disabled(line)
        for line in lines
    )
    disabled_checks_are_described = not any(bitcoin_disabled(line) for line in lines) or any(
        manual_check(line) for line in lines
    )
    return bool(lines) and allowed and has_deferred_status and disabled_checks_are_described


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
            manifest_path = root / "manifest.json"
            proof_path = root / "manifest.json.ots"
            manifest_path.write_bytes(manifest_bytes)
            proof_path.write_bytes(proof_bytes)
            proc = subprocess.run(
                [*self.command, "verify", str(proof_path), "-f", str(manifest_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        if proc.returncode != 0 and not _is_nonfatal_timestamp_status(
            proc.stdout,
            proc.stderr,
        ):
            raise ProofVerifyError(proc.stderr or proc.stdout or "proof verification failed")


@dataclass(frozen=True)
class CommandProofUpgrader:
    command: Sequence[str] = ("ots",)

    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult:
        if not self.command:
            raise ProofUpgradeError("proof upgrade command is empty")
        with tempfile.TemporaryDirectory(prefix="riverhog-ots-upgrade-") as tmp:
            proof_path = Path(tmp) / "manifest.json.ots"
            proof_path.write_bytes(proof_bytes)
            proc = subprocess.run(
                [*self.command, "upgrade", str(proof_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                output = "\n".join((proc.stdout, proc.stderr))
                if "Failed! Timestamp not complete" in output and "Error!" not in output:
                    return ProofUpgradeResult(proof_bytes=proof_bytes, complete=False)
                raise ProofUpgradeError(proc.stderr or proc.stdout or "proof upgrade failed")
            if not proof_path.exists():
                raise ProofUpgradeError("proof upgrade command removed the .ots file")
            return ProofUpgradeResult(proof_bytes=proof_path.read_bytes(), complete=True)
