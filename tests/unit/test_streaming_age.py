from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from riverhog_age import CHUNK_SIZE, ResumableAgeScryptSession
from riverhog_core.streaming_age import (
    ResumableAgeSessionCache,
    prepare_age_part,
)


def test_age_session_cache_single_flights_concurrent_scrypt_derivation(monkeypatch) -> None:
    state = ResumableAgeScryptSession.create(
        "archive passphrase",
        log_n=1,
        plaintext_size=123,
    ).export_state(plaintext_size=123)
    original = ResumableAgeScryptSession.from_state
    count = 0
    lock = threading.Lock()

    def wrapped(passphrase, upload_state, **kwargs):
        nonlocal count
        with lock:
            count += 1
        time.sleep(0.02)
        return original(passphrase, upload_state, **kwargs)

    monkeypatch.setattr(ResumableAgeScryptSession, "from_state", wrapped)
    cache = ResumableAgeSessionCache("archive passphrase", max_entries=8)
    with ThreadPoolExecutor(max_workers=8) as executor:
        sessions = list(executor.map(lambda _value: cache.get(state), range(8)))

    assert count == 1
    assert all(current is sessions[0] for current in sessions)


def test_prepare_age_part_consumes_large_source_chunk_without_plaintext_part_staging() -> None:
    content = bytes(index % 251 for index in range((128 * CHUNK_SIZE) + 17))
    session = ResumableAgeScryptSession.create(
        "archive passphrase",
        log_n=1,
        plaintext_size=len(content),
    )
    plan = session.age_aligned_unit_plans(
        len(content),
        chunks_per_unit=256,
    )[0]

    prepared = prepare_age_part(
        session=session,
        plan=plan,
        total_plaintext_bytes=len(content),
        plaintext_chunks=(content,),
    )

    assert prepared.plaintext_bytes == len(content)
    assert prepared.plaintext_sha256 == hashlib.sha256(content).hexdigest()
    assert prepared.ciphertext == session.encrypt_plaintext(content)


def test_age_session_cache_bounds_distinct_scrypt_derivations(monkeypatch) -> None:
    from riverhog_core.throughput import TransferConcurrencyGate

    states = tuple(
        ResumableAgeScryptSession.create(
            "archive passphrase",
            log_n=1,
            plaintext_size=index + 1,
        ).export_state(plaintext_size=index + 1)
        for index in range(4)
    )
    original = ResumableAgeScryptSession.from_state
    lock = threading.Lock()
    active = 0
    maximum = 0

    def wrapped(passphrase, upload_state, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        try:
            return original(passphrase, upload_state, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(ResumableAgeScryptSession, "from_state", wrapped)
    cache = ResumableAgeSessionCache(
        "archive passphrase",
        max_entries=8,
        derivation_gate=TransferConcurrencyGate(1),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(cache.get, states))

    assert maximum == 1
