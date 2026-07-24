from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from riverhog_age import AEAD_TAG_SIZE, CHUNK_SIZE, ResumableAgeScryptSession, UploadState
from riverhog_protocol.ingress import INGRESS_ENCRYPTION_FORMAT


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
    session = ResumableAgeScryptSession.from_state(
        str(descriptor["passphrase"]),
        UploadState.from_json_bytes(json.dumps(dict(state_value))),
    )
    chunks_per_part = max(
        1,
        (target_part_bytes - len(session.age_prefix)) // (CHUNK_SIZE + AEAD_TAG_SIZE),
    )
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
            part_offset = plan.ciphertext_start
            plaintext_start = plan.plaintext_start
            plaintext_len = plan.plaintext_len
            if plan is plans[start_index] and ciphertext_offset > plan.ciphertext_start:
                skipped_ciphertext = ciphertext_offset - plan.ciphertext_start
                payload_skip = skipped_ciphertext
                if plan.includes_age_prefix:
                    prefix_bytes = len(session.age_prefix)
                    payload_skip = max(0, skipped_ciphertext - prefix_bytes)
                skipped_chunks = payload_skip // (CHUNK_SIZE + AEAD_TAG_SIZE)
                skipped_plaintext = min(skipped_chunks * CHUNK_SIZE, plaintext_len)
                encrypted = encrypted[skipped_ciphertext:]
                part_offset = ciphertext_offset
                plaintext_start += skipped_plaintext
                plaintext_len -= skipped_plaintext
            yield IngressUploadPart(
                ciphertext_offset=part_offset,
                ciphertext=encrypted,
                plaintext_start=plaintext_start,
                plaintext_bytes=plaintext_len,
            )
