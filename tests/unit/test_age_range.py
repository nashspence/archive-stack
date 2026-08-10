from __future__ import annotations

from dataclasses import replace

import pytest
from riverhog_age import CHUNK_SIZE, ResumableAgeScryptSession
from riverhog_core.age_range import (
    AgePlaintextRange,
    iter_decrypt_age_plaintext_range,
    plan_age_plaintext_range,
)


def _encrypted(content: bytes) -> tuple[str, bytes]:
    session = ResumableAgeScryptSession.create(
        "archive passphrase",
        log_n=1,
        plaintext_size=len(content),
    )
    state = session.export_state(plaintext_size=len(content)).to_json_bytes().decode("utf-8")
    return state, session.encrypt_plaintext(content)


def _decrypt(state: str, plan: AgePlaintextRange, ciphertext: bytes) -> bytes:
    return b"".join(
        iter_decrypt_age_plaintext_range(
            passphrase="archive passphrase",
            age_state=state,
            plan=plan,
            ciphertext_chunks=(ciphertext,),
        )
    )


def test_authenticated_age_range_recovers_only_requested_plaintext() -> None:
    content = bytes(index % 251 for index in range((3 * CHUNK_SIZE) + 903))
    state, ciphertext = _encrypted(content)
    offset = CHUNK_SIZE - 17
    length = CHUNK_SIZE + 1234
    plan = plan_age_plaintext_range(
        age_state=state,
        total_plaintext_bytes=len(content),
        plaintext_offset=offset,
        plaintext_bytes=length,
    )

    recovered = _decrypt(
        state,
        plan,
        ciphertext[plan.ciphertext_offset : plan.ciphertext_end],
    )

    assert recovered == content[offset : offset + length]
    assert plan.chunk_count == 3
    assert plan.ciphertext_bytes < len(ciphertext)


def test_authenticated_age_range_rejects_corruption_and_plan_tampering() -> None:
    content = b"x" * (2 * CHUNK_SIZE)
    state, ciphertext = _encrypted(content)
    plan = plan_age_plaintext_range(
        age_state=state,
        total_plaintext_bytes=len(content),
        plaintext_offset=100,
        plaintext_bytes=10_000,
    )
    selected = bytearray(ciphertext[plan.ciphertext_offset : plan.ciphertext_end])
    selected[-1] ^= 1

    with pytest.raises(ValueError, match="authentication"):
        _decrypt(state, plan, bytes(selected))
    with pytest.raises(ValueError, match="inconsistent"):
        _decrypt(state, replace(plan, plaintext_bytes=plan.plaintext_bytes - 1), b"")


def test_authenticated_age_range_consumes_one_large_range_chunk_efficiently() -> None:
    content = bytes(index % 239 for index in range((130 * CHUNK_SIZE) + 31))
    state, ciphertext = _encrypted(content)
    offset = 7
    length = len(content) - 19
    plan = plan_age_plaintext_range(
        age_state=state,
        total_plaintext_bytes=len(content),
        plaintext_offset=offset,
        plaintext_bytes=length,
    )

    recovered = _decrypt(
        state,
        plan,
        ciphertext[plan.ciphertext_offset : plan.ciphertext_end],
    )

    assert recovered == content[offset : offset + length]
