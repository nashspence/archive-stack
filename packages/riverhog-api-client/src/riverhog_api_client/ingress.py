from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from riverhog_age import (
    AEAD_TAG_SIZE,
    CHUNK_SIZE,
    ResumableAgeScryptSession,
    UploadState,
    plaintext_bytes_for_ciphertext_offset,
)
from riverhog_protocol.ingress import INGRESS_ENCRYPTION_FORMAT

DEFAULT_INGRESS_PART_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class IngressUploadPart:
    ciphertext_offset: int
    ciphertext: bytes
    plaintext_start: int
    plaintext_bytes: int


def iter_ingress_upload_parts(
    source_path: Path,
    descriptor: Mapping[str, object],
    *,
    ciphertext_offset: int,
    target_part_bytes: int,
) -> Iterator[IngressUploadPart]:
    if str(descriptor.get("format")) != INGRESS_ENCRYPTION_FORMAT:
        raise ValueError("unsupported ingress encryption format")
    state_value = descriptor.get("state")
    if not isinstance(state_value, Mapping):
        raise ValueError("ingress encryption state is invalid")
    plaintext_bytes = int(str(descriptor.get("plaintext_bytes", -1)))
    ciphertext_bytes = int(str(descriptor.get("ciphertext_bytes", -1)))
    if plaintext_bytes < 0 or ciphertext_bytes < 0:
        raise ValueError("ingress encryption lengths are invalid")
    if source_path.stat().st_size != plaintext_bytes:
        raise ValueError("ingress upload source length changed")
    if target_part_bytes <= 0:
        raise ValueError("ingress upload part size must be positive")
    session = ResumableAgeScryptSession.from_state(
        str(descriptor["passphrase"]),
        UploadState.from_json_bytes(json.dumps(dict(state_value))),
    )
    chunks_per_part = max(1, min(64, target_part_bytes // (CHUNK_SIZE + AEAD_TAG_SIZE)))
    plans = session.s3_part_plans(
        plaintext_bytes,
        chunks_per_part=chunks_per_part,
        enforce_s3_limits=False,
    )
    if ciphertext_offset == ciphertext_bytes:
        return
    if ciphertext_offset < 0 or ciphertext_offset > ciphertext_bytes:
        raise ValueError("ingress upload offset is outside ciphertext bounds")
    start_index = next(
        (
            index
            for index, plan in enumerate(plans)
            if plan.ciphertext_start <= ciphertext_offset < plan.ciphertext_end
        ),
        None,
    )
    if start_index is None:
        raise ValueError("ingress upload offset is not covered by the encryption plan")

    def completed_plaintext(offset: int) -> int:
        return plaintext_bytes_for_ciphertext_offset(
            state=state_value,
            plaintext_bytes=plaintext_bytes,
            ciphertext_bytes=ciphertext_bytes,
            ciphertext_offset=offset,
        )

    next_offset = ciphertext_offset
    request_bytes = target_part_bytes - (next_offset % target_part_bytes)
    pending = bytearray()
    with source_path.open("rb") as source:
        for plan in plans[start_index:]:

            def provider(_chunk_index: int, start: int, end: int) -> bytes:
                source.seek(start)
                return source.read(end - start)

            encrypted = session.encrypt_part(
                plan,
                provider,
                plaintext_size=plaintext_bytes,
            )
            if plan is plans[start_index] and ciphertext_offset > plan.ciphertext_start:
                skipped_ciphertext = ciphertext_offset - plan.ciphertext_start
                encrypted = encrypted[skipped_ciphertext:]
            cursor = 0
            while cursor < len(encrypted):
                take = min(request_bytes - len(pending), len(encrypted) - cursor)
                pending.extend(encrypted[cursor : cursor + take])
                cursor += take
                if len(pending) != request_bytes:
                    continue
                part_end = next_offset + len(pending)
                plaintext_start = completed_plaintext(next_offset)
                plaintext_end = completed_plaintext(part_end)
                yield IngressUploadPart(
                    ciphertext_offset=next_offset,
                    ciphertext=bytes(pending),
                    plaintext_start=plaintext_start,
                    plaintext_bytes=plaintext_end - plaintext_start,
                )
                next_offset = part_end
                request_bytes = target_part_bytes
                pending.clear()
    if pending:
        part_end = next_offset + len(pending)
        plaintext_start = completed_plaintext(next_offset)
        plaintext_end = completed_plaintext(part_end)
        yield IngressUploadPart(
            ciphertext_offset=next_offset,
            ciphertext=bytes(pending),
            plaintext_start=plaintext_start,
            plaintext_bytes=plaintext_end - plaintext_start,
        )
