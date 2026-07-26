from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from riverhog_age import (
    AEAD_TAG_SIZE,
    CHUNK_SIZE,
    ResumableAgeScryptSession,
    UploadState,
    age_chunk_count_for_plaintext_len,
    age_ciphertext_len_for_plaintext_len,
)
from riverhog_protocol.ingress import INGRESS_ENCRYPTION_FORMAT

from riverhog_core.ports.upload_store import UploadStore
from riverhog_core.runtime_config import RuntimeConfig

INGRESS_SCRYPT_LOG_N = 1


@dataclass(frozen=True, slots=True)
class IngressEncryption:
    secret_envelope: str
    state_json: str
    ciphertext_bytes: int


def create_ingress_encryption(
    config: RuntimeConfig,
    *,
    collection_id: int,
    path: str,
    plaintext_bytes: int,
) -> IngressEncryption:
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    session = ResumableAgeScryptSession.create(
        secret,
        log_n=INGRESS_SCRYPT_LOG_N,
        plaintext_size=plaintext_bytes,
    )
    state_json = (
        session.export_state(plaintext_size=plaintext_bytes).to_json_bytes().decode("utf-8")
    )
    return IngressEncryption(
        secret_envelope=_wrap_secret(
            config,
            collection_id=collection_id,
            path=path,
            secret=secret,
        ),
        state_json=state_json,
        ciphertext_bytes=age_ciphertext_len_for_plaintext_len(
            plaintext_bytes,
            age_prefix_len=len(session.age_prefix),
        ),
    )


def ingress_encryption_descriptor(
    config: RuntimeConfig,
    *,
    collection_id: int,
    path: str,
    plaintext_bytes: int,
    ciphertext_bytes: int,
    secret_envelope: str,
    state_json: str,
) -> dict[str, object]:
    secret = _unwrap_secret(
        config,
        collection_id=collection_id,
        path=path,
        envelope=secret_envelope,
    )
    return {
        "format": INGRESS_ENCRYPTION_FORMAT,
        "passphrase": secret,
        "state": json.loads(state_json),
        "plaintext_bytes": plaintext_bytes,
        "ciphertext_bytes": ciphertext_bytes,
        "chunk_bytes": CHUNK_SIZE,
    }


def iter_ingress_plaintext(
    config: RuntimeConfig,
    upload_store: UploadStore,
    *,
    target_path: str,
    collection_id: int,
    path: str,
    plaintext_bytes: int,
    secret_envelope: str,
    state_json: str,
    offset: int = 0,
    size: int | None = None,
) -> Iterator[bytes]:
    if offset < 0 or offset > plaintext_bytes:
        raise ValueError("ingress plaintext offset is outside the file")
    requested = plaintext_bytes - offset if size is None else size
    if requested < 0 or offset + requested > plaintext_bytes:
        raise ValueError("ingress plaintext range is outside the file")
    if requested == 0:
        return
    secret = _unwrap_secret(
        config,
        collection_id=collection_id,
        path=path,
        envelope=secret_envelope,
    )
    state = UploadState.from_json_bytes(state_json)
    session = ResumableAgeScryptSession.from_state(secret, state)
    first_chunk = offset // CHUNK_SIZE
    final_byte = offset + requested
    last_chunk = (final_byte - 1) // CHUNK_SIZE
    chunk_count = age_chunk_count_for_plaintext_len(plaintext_bytes)
    ciphertext_start = len(session.age_prefix) + first_chunk * (CHUNK_SIZE + AEAD_TAG_SIZE)
    ciphertext_end = len(session.age_prefix)
    for chunk_index in range(last_chunk + 1):
        plain_start = chunk_index * CHUNK_SIZE
        plain_end = min(plain_start + CHUNK_SIZE, plaintext_bytes)
        ciphertext_end += plain_end - plain_start + AEAD_TAG_SIZE
    encrypted = iter(
        upload_store.iter_target(
            target_path,
            offset=ciphertext_start,
            size=ciphertext_end - ciphertext_start,
        )
    )
    buffer = bytearray()
    for chunk_index in range(first_chunk, last_chunk + 1):
        plain_start = chunk_index * CHUNK_SIZE
        plain_end = min(plain_start + CHUNK_SIZE, plaintext_bytes)
        encrypted_size = plain_end - plain_start + AEAD_TAG_SIZE
        while len(buffer) < encrypted_size:
            try:
                buffer.extend(next(encrypted))
            except StopIteration as exc:
                raise ValueError("encrypted ingress object ended before its declared size") from exc
        ciphertext = bytes(buffer[:encrypted_size])
        del buffer[:encrypted_size]
        plaintext = session.decrypt_chunk(
            chunk_index,
            ciphertext,
            final=chunk_index == chunk_count - 1,
        )
        emit_start = max(offset - plain_start, 0)
        emit_end = min(final_byte - plain_start, len(plaintext))
        if emit_start < emit_end:
            yield plaintext[emit_start:emit_end]


def _envelope_key(config: RuntimeConfig) -> bytes:
    return hashlib.sha256(
        b"riverhog-ingress-envelope-v1\0" + config.ingress_secret_key.encode("utf-8")
    ).digest()


def _aad(collection_id: int, path: str) -> bytes:
    return f"{collection_id}\0{path}".encode()


def _wrap_secret(
    config: RuntimeConfig,
    *,
    collection_id: int,
    path: str,
    secret: str,
) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_envelope_key(config)).encrypt(
        nonce,
        secret.encode("ascii"),
        _aad(collection_id, path),
    )
    return "v1." + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")


def _unwrap_secret(
    config: RuntimeConfig,
    *,
    collection_id: int,
    path: str,
    envelope: str,
) -> str:
    version, separator, encoded = envelope.partition(".")
    if version != "v1" or not separator:
        raise ValueError("unsupported ingress-secret envelope")
    padded = encoded + "=" * (-len(encoded) % 4)
    payload = base64.urlsafe_b64decode(padded.encode("ascii"))
    if len(payload) < 13:
        raise ValueError("invalid ingress-secret envelope")
    plaintext = AESGCM(_envelope_key(config)).decrypt(
        payload[:12],
        payload[12:],
        _aad(collection_id, path),
    )
    return plaintext.decode("ascii")
