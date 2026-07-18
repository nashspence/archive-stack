from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from riverhog_age import AEAD_TAG_SIZE, CHUNK_SIZE, ResumableAgeScryptSession, UploadState
from riverhog_core.ingress_crypto import INGRESS_ENCRYPTION_FORMAT


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
    plan_index = {plan.ciphertext_start: index for index, plan in enumerate(plans)}
    if ciphertext_offset == ciphertext_bytes:
        return
    start_index = plan_index.get(ciphertext_offset)
    if start_index is None:
        raise ValueError("ingress upload offset is not on an encryption boundary")

    with source_path.open("rb") as source:
        for plan in plans[start_index:]:

            def provider(_chunk_index: int, start: int, end: int) -> bytes:
                source.seek(start)
                return source.read(end - start)

            yield IngressUploadPart(
                ciphertext_offset=plan.ciphertext_start,
                ciphertext=session.encrypt_part(
                    plan,
                    provider,
                    plaintext_size=plaintext_bytes,
                ),
                plaintext_start=plan.plaintext_start,
                plaintext_bytes=plan.plaintext_len,
            )
