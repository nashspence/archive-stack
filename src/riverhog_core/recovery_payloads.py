from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

RECOVERY_PAYLOAD_ALG = "age-plugin-batchpass/v1"


class RecoveryPayloadError(ValueError):
    pass


class RecoveryPayloadCodec(Protocol):
    @property
    def metadata(self) -> Mapping[str, object]: ...

    def encrypt(self, content: bytes) -> bytes: ...

    def decrypt(self, content: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CommandAgeBatchpassRecoveryPayloadCodec:
    command: Sequence[str]
    passphrase: str
    work_factor: int = 18
    max_work_factor: int = 30

    @property
    def metadata(self) -> Mapping[str, object]:
        return {
            "alg": RECOVERY_PAYLOAD_ALG,
            "work_factor": self.work_factor,
        }

    def encrypt(self, content: bytes) -> bytes:
        return self._run(
            ("-e", "-j", "batchpass"),
            content,
            extra_env={"AGE_PASSPHRASE_WORK_FACTOR": str(self.work_factor)},
            operation="encrypt",
        )

    def decrypt(self, content: bytes) -> bytes:
        return self._run(
            ("-d", "-j", "batchpass"),
            content,
            extra_env={"AGE_PASSPHRASE_MAX_WORK_FACTOR": str(self.max_work_factor)},
            operation="decrypt",
        )

    def _run(
        self,
        args: Sequence[str],
        content: bytes,
        *,
        extra_env: Mapping[str, str],
        operation: str,
    ) -> bytes:
        if not self.command:
            raise RecoveryPayloadError("recovery payload age command is empty")
        if not self.passphrase:
            raise RecoveryPayloadError("recovery payload passphrase is not configured")
        env = {
            **os.environ,
            **extra_env,
            "AGE_PASSPHRASE": self.passphrase,
        }
        try:
            proc = subprocess.run(
                [*self.command, *args],
                input=content,
                capture_output=True,
                check=False,
                env=env,
            )
        except OSError as exc:
            raise RecoveryPayloadError(f"recovery payload {operation} command failed") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
            if not detail:
                detail = f"age command exited with status {proc.returncode}"
            raise RecoveryPayloadError(f"recovery payload {operation} failed: {detail}")
        return proc.stdout


def encrypt_recovery_payload(content: bytes, codec: RecoveryPayloadCodec) -> bytes:
    return codec.encrypt(content)


def decrypt_recovery_payload(content: bytes, codec: RecoveryPayloadCodec) -> bytes:
    return codec.decrypt(content)
