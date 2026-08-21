"""
Passphrase-only, standard age v1 encryption with explicit resumable chunk state.

This module is intentionally small and Riverhog-oriented. It supports:

* standard age v1 scrypt recipient files, no compression, no armor;
* deterministic regeneration of payload chunk ciphertext once a session is created;
* export/import of upload-session state without persisting the plaintext file key;
* portable multipart planning that aligns every unit to age chunk boundaries.

It does NOT implement X25519 recipients, plugins, armor, or general age CLI UX.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

AGE_V1_LINE = b"age-encryption.org/v1\n"
SCRYPT_SALT_PREFIX = b"age-encryption.org/v1/scrypt"
HEADER_HKDF_INFO = b"header"
PAYLOAD_HKDF_INFO = b"payload"
CHUNK_SIZE = 64 * 1024
AEAD_TAG_SIZE = 16
PAYLOAD_NONCE_SIZE = 16
FILE_KEY_SIZE = 16
SCRYPT_SALT_SIZE = 16
ZERO_AEAD_NONCE = b"\x00" * 12
DEFAULT_SCRYPT_LOG_N = 18
PORTABLE_MULTIPART_MIN_PART_BYTES = 5 * 1024 * 1024
PORTABLE_MULTIPART_MAX_PART_BYTES = 5 * 1024 * 1024 * 1024
PORTABLE_MULTIPART_MAX_PARTS = 10_000
# 1024 age chunks is about 64 MiB per part, safely above the 5 MiB
# minimum and low enough to keep 70--500 GiB archives under 10,000 parts.
DEFAULT_CHUNKS_PER_MULTIPART_PART = 1024


class AgeFormatError(ValueError):
    """Raised when age header/payload structure is invalid."""


class AgeDecryptError(ValueError):
    """Raised when authentication or passphrase-based unwrapping fails."""


@dataclass(frozen=True)
class UploadState:
    """
    Serializable state needed to resume an age-encrypted object upload.

    The header contains the encrypted file key. With batchpass/passphrase available,
    Riverhog can rederive the file key on resume without storing it in plaintext.
    """

    header: bytes
    payload_nonce: bytes
    plaintext_size: int | None = None
    format: str = "age-v1-scrypt-resumable"

    def to_json_bytes(self) -> bytes:
        payload = {
            "format": self.format,
            "header_b64": _b64_encode(self.header),
            "payload_nonce_b64": _b64_encode(self.payload_nonce),
            "plaintext_size": self.plaintext_size,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, data: bytes | str) -> UploadState:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        raw = json.loads(data)
        if raw.get("format") != "age-v1-scrypt-resumable":
            raise AgeFormatError(f"unsupported upload state format: {raw.get('format')!r}")
        header = _b64_decode(raw["header_b64"])
        payload_nonce = _b64_decode(raw["payload_nonce_b64"])
        plaintext_size = raw.get("plaintext_size")
        if plaintext_size is not None and (
            not isinstance(plaintext_size, int) or plaintext_size < 0
        ):
            raise AgeFormatError("plaintext_size must be a non-negative integer or null")
        if len(payload_nonce) != PAYLOAD_NONCE_SIZE:
            raise AgeFormatError("payload nonce must be 16 bytes")
        return cls(header=header, payload_nonce=payload_nonce, plaintext_size=plaintext_size)


def plaintext_bytes_for_ciphertext_offset(
    *,
    state: UploadState | bytes | str | Mapping[str, object],
    plaintext_bytes: int,
    ciphertext_bytes: int,
    ciphertext_offset: int,
) -> int:
    """Map an age ciphertext offset to fully completed plaintext bytes."""

    if ciphertext_offset <= 0:
        return 0
    if ciphertext_offset >= ciphertext_bytes:
        return plaintext_bytes
    if isinstance(state, UploadState):
        upload_state = state
    elif isinstance(state, (bytes, str)):
        upload_state = UploadState.from_json_bytes(state)
    else:
        upload_state = UploadState.from_json_bytes(json.dumps(dict(state)))
    prefix_bytes = len(upload_state.header) + len(upload_state.payload_nonce)
    if ciphertext_offset <= prefix_bytes:
        return 0
    payload_offset = ciphertext_offset - prefix_bytes
    encrypted_chunk_bytes = CHUNK_SIZE + AEAD_TAG_SIZE
    complete_chunks = payload_offset // encrypted_chunk_bytes
    return min(complete_chunks * CHUNK_SIZE, plaintext_bytes)


@dataclass(frozen=True)
class MultipartPartPlan:
    """A portable multipart unit whose contents align to age chunk boundaries."""

    part_number: int
    first_chunk: int
    chunk_count: int
    includes_age_prefix: bool
    plaintext_start: int
    plaintext_end: int
    ciphertext_start: int
    ciphertext_end: int

    @property
    def plaintext_len(self) -> int:
        return self.plaintext_end - self.plaintext_start

    @property
    def ciphertext_len(self) -> int:
        return self.ciphertext_end - self.ciphertext_start


@dataclass(frozen=True)
class ParsedScryptHeader:
    header: bytes
    salt: bytes
    log_n: int
    body: bytes
    mac: bytes
    mac_input: bytes


class ResumableAgeScryptSession:
    """
    A standard age v1 passphrase session with direct chunk encryption.

    Create one session when an archive upload starts, persist `export_state()`, and
    reconstruct with `from_state(passphrase, state)` when resuming.
    """

    def __init__(self, *, header: bytes, payload_nonce: bytes, file_key: bytes):
        if len(file_key) != FILE_KEY_SIZE:
            raise ValueError("file_key must be 16 bytes")
        if len(payload_nonce) != PAYLOAD_NONCE_SIZE:
            raise ValueError("payload_nonce must be 16 bytes")
        self.header = header
        self.payload_nonce = payload_nonce
        self._file_key = file_key
        self._payload_key = _hkdf_sha256(file_key, salt=payload_nonce, info=PAYLOAD_HKDF_INFO)

    @classmethod
    def create(
        cls,
        passphrase: str | bytes,
        *,
        log_n: int = DEFAULT_SCRYPT_LOG_N,
        plaintext_size: int | None = None,
        file_key: bytes | None = None,
        scrypt_salt: bytes | None = None,
        payload_nonce: bytes | None = None,
        scrypt_maxmem: int | None = None,
    ) -> ResumableAgeScryptSession:
        """
        Create a new standard age v1 passphrase-encrypted stream session.

        `file_key`, `scrypt_salt`, and `payload_nonce` are injectable for tests only;
        production callers should leave them as None.
        """

        _validate_log_n(log_n)
        passphrase_bytes = _passphrase_to_bytes(passphrase)
        file_key = os.urandom(FILE_KEY_SIZE) if file_key is None else file_key
        scrypt_salt = os.urandom(SCRYPT_SALT_SIZE) if scrypt_salt is None else scrypt_salt
        payload_nonce = os.urandom(PAYLOAD_NONCE_SIZE) if payload_nonce is None else payload_nonce
        if len(file_key) != FILE_KEY_SIZE:
            raise ValueError("file_key must be 16 bytes")
        if len(scrypt_salt) != SCRYPT_SALT_SIZE:
            raise ValueError("scrypt_salt must be 16 bytes")
        if len(payload_nonce) != PAYLOAD_NONCE_SIZE:
            raise ValueError("payload_nonce must be 16 bytes")

        wrap_key = _derive_scrypt_wrap_key(
            passphrase_bytes, scrypt_salt, log_n, maxmem=scrypt_maxmem
        )
        body = ChaCha20Poly1305(wrap_key).encrypt(ZERO_AEAD_NONCE, file_key, b"")
        header = _build_scrypt_header(scrypt_salt, log_n, body, file_key)
        return cls(header=header, payload_nonce=payload_nonce, file_key=file_key)

    @classmethod
    def from_state(
        cls,
        passphrase: str | bytes,
        state: UploadState | bytes | str | Mapping[str, object],
        *,
        scrypt_maxmem: int | None = None,
    ) -> ResumableAgeScryptSession:
        """
        Restore a session from persisted upload state and the batch passphrase.
        """

        if isinstance(state, UploadState):
            upload_state = state
        elif isinstance(state, (bytes, str)):
            upload_state = UploadState.from_json_bytes(state)
        else:
            upload_state = UploadState.from_json_bytes(json.dumps(dict(state)).encode("utf-8"))
        parsed = parse_scrypt_header(upload_state.header)
        file_key = _unwrap_scrypt_file_key(
            _passphrase_to_bytes(passphrase), parsed, scrypt_maxmem=scrypt_maxmem
        )
        return cls(
            header=upload_state.header,
            payload_nonce=upload_state.payload_nonce,
            file_key=file_key,
        )

    def export_state(self, *, plaintext_size: int | None = None) -> UploadState:
        if plaintext_size is not None and plaintext_size < 0:
            raise ValueError("plaintext_size must be non-negative")
        return UploadState(
            header=self.header,
            payload_nonce=self.payload_nonce,
            plaintext_size=plaintext_size,
        )

    @property
    def age_prefix(self) -> bytes:
        """The bytes before the first encrypted payload chunk: header || payload nonce."""

        return self.header + self.payload_nonce

    def encrypt_chunk(self, chunk_index: int, plaintext: bytes, *, final: bool) -> bytes:
        """Encrypt exactly one standard age payload chunk."""

        if chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if chunk_index >= 1 << (8 * 11):
            raise ValueError("chunk_index does not fit the age 11-byte chunk counter")
        if len(plaintext) > CHUNK_SIZE:
            raise ValueError("age plaintext chunks must be at most 64 KiB")
        nonce = _chunk_nonce(chunk_index, final=final)
        return ChaCha20Poly1305(self._payload_key).encrypt(nonce, plaintext, b"")

    def decrypt_chunk(self, chunk_index: int, ciphertext: bytes, *, final: bool) -> bytes:
        """Decrypt one authenticated payload chunk from this session."""

        if chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if len(ciphertext) < AEAD_TAG_SIZE or len(ciphertext) > CHUNK_SIZE + AEAD_TAG_SIZE:
            raise AgeFormatError("age payload chunk has an invalid ciphertext length")
        try:
            plaintext = ChaCha20Poly1305(self._payload_key).decrypt(
                _chunk_nonce(chunk_index, final=final),
                ciphertext,
                b"",
            )
        except InvalidTag as exc:
            raise AgeDecryptError("age payload authentication failed") from exc
        if not final and len(plaintext) != CHUNK_SIZE:
            raise AgeFormatError("non-final age payload chunk decrypted to non-64KiB plaintext")
        return plaintext

    def encrypt_plaintext(self, plaintext: bytes) -> bytes:
        """Encrypt a complete plaintext into a standard binary .age file."""

        out = bytearray(self.age_prefix)
        chunks = split_plaintext_chunks(len(plaintext))
        for idx, start, end, final in chunks:
            out.extend(self.encrypt_chunk(idx, plaintext[start:end], final=final))
        return bytes(out)

    def multipart_part_plans(
        self,
        plaintext_size: int,
        *,
        chunks_per_part: int = DEFAULT_CHUNKS_PER_MULTIPART_PART,
        enforce_portable_limits: bool = True,
    ) -> list[MultipartPartPlan]:
        return make_multipart_part_plans(
            plaintext_size,
            age_prefix_len=len(self.age_prefix),
            chunks_per_part=chunks_per_part,
            enforce_portable_limits=enforce_portable_limits,
        )

    def encrypt_part(
        self,
        plan: MultipartPartPlan,
        plaintext_chunk_provider: Callable[[int, int, int], bytes],
        *,
        plaintext_size: int,
    ) -> bytes:
        """
        Encrypt bytes for one multipart unit from deterministic plaintext chunks.

        `plaintext_chunk_provider(chunk_index, start, end)` must return plaintext
        bytes for [start:end] in the original tar stream.
        """

        chunk_count = age_chunk_count_for_plaintext_len(plaintext_size)
        out = bytearray()
        if plan.includes_age_prefix:
            out.extend(self.age_prefix)
        for chunk_index in range(plan.first_chunk, plan.first_chunk + plan.chunk_count):
            start = chunk_index * CHUNK_SIZE
            end = min(start + CHUNK_SIZE, plaintext_size)
            if plaintext_size == 0:
                start = end = 0
            final = chunk_index == chunk_count - 1
            chunk = plaintext_chunk_provider(chunk_index, start, end)
            expected = end - start
            if len(chunk) != expected:
                raise ValueError(
                    f"chunk provider returned {len(chunk)} bytes for chunk "
                    f"{chunk_index}; expected {expected}"
                )
            out.extend(self.encrypt_chunk(chunk_index, chunk, final=final))
        if len(out) != plan.ciphertext_len:
            raise RuntimeError(
                f"internal part length mismatch: got {len(out)}, planned {plan.ciphertext_len}"
            )
        return bytes(out)


def encrypt_age_scrypt(
    plaintext: bytes,
    passphrase: str | bytes,
    *,
    log_n: int = DEFAULT_SCRYPT_LOG_N,
    scrypt_maxmem: int | None = None,
) -> bytes:
    """Convenience API for small files like manifest.json and proof.ots."""

    return ResumableAgeScryptSession.create(
        passphrase, log_n=log_n, scrypt_maxmem=scrypt_maxmem
    ).encrypt_plaintext(plaintext)


def decrypt_age_scrypt(
    age_file: bytes,
    passphrase: str | bytes,
    *,
    scrypt_maxmem: int | None = None,
) -> bytes:
    """Decrypt a binary standard age v1 scrypt file produced by this module."""

    try:
        parsed = parse_scrypt_header_from_age_file(age_file)
    except AgeFormatError as exc:
        if _age_header_has_no_scrypt_recipient(age_file):
            raise AgeDecryptError("no supported scrypt recipient stanza found") from exc
        raise
    file_key = _unwrap_scrypt_file_key(
        _passphrase_to_bytes(passphrase),
        parsed,
        scrypt_maxmem=scrypt_maxmem,
    )
    payload_offset = len(parsed.header)
    if len(age_file) < payload_offset + PAYLOAD_NONCE_SIZE:
        raise AgeFormatError("age payload is missing 16-byte nonce")
    payload_nonce = age_file[payload_offset : payload_offset + PAYLOAD_NONCE_SIZE]
    ciphertext = age_file[payload_offset + PAYLOAD_NONCE_SIZE :]
    return decrypt_payload_chunks(file_key, payload_nonce, ciphertext)


def iter_decrypt_age_scrypt(
    chunks: Iterable[bytes],
    passphrase: str | bytes,
    *,
    scrypt_maxmem: int | None = None,
) -> Iterator[bytes]:
    """
    Decrypt a binary standard age v1 scrypt stream without materializing it.

    The STREAM payload marks the final chunk in the nonce, so this keeps one
    encrypted chunk buffered until EOF before deciding whether it is final.
    """

    source = iter(chunks)
    prefix = bytearray()
    header: bytes | None = None
    while header is None:
        try:
            prefix.extend(next(source))
        except StopIteration as exc:
            raise AgeFormatError("age header is missing MAC line") from exc
        header_end = _age_header_end(prefix)
        if header_end is not None:
            header = bytes(prefix[:header_end])
            del prefix[:header_end]

    try:
        parsed = parse_scrypt_header(header)
    except AgeFormatError as exc:
        raise exc
    file_key = _unwrap_scrypt_file_key(
        _passphrase_to_bytes(passphrase),
        parsed,
        scrypt_maxmem=scrypt_maxmem,
    )

    while len(prefix) < PAYLOAD_NONCE_SIZE:
        try:
            prefix.extend(next(source))
        except StopIteration as exc:
            raise AgeFormatError("age payload is missing 16-byte nonce") from exc
    payload_nonce = bytes(prefix[:PAYLOAD_NONCE_SIZE])
    del prefix[:PAYLOAD_NONCE_SIZE]
    yield from iter_decrypt_payload_chunks(file_key, payload_nonce, _prepend(prefix, source))


def decrypt_payload_chunks(file_key: bytes, payload_nonce: bytes, ciphertext: bytes) -> bytes:
    """Decrypt age payload ciphertext after the 16-byte payload nonce."""

    if len(file_key) != FILE_KEY_SIZE:
        raise ValueError("file_key must be 16 bytes")
    if len(payload_nonce) != PAYLOAD_NONCE_SIZE:
        raise ValueError("payload_nonce must be 16 bytes")
    if len(ciphertext) < AEAD_TAG_SIZE:
        raise AgeFormatError("age payload must include at least one final chunk tag")

    payload_key = _hkdf_sha256(file_key, salt=payload_nonce, info=PAYLOAD_HKDF_INFO)
    aead = ChaCha20Poly1305(payload_key)
    out = bytearray()
    offset = 0
    chunk_index = 0
    max_nonfinal_len = CHUNK_SIZE + AEAD_TAG_SIZE

    while offset < len(ciphertext):
        remaining = len(ciphertext) - offset
        final = remaining <= max_nonfinal_len
        if not final:
            chunk_ct = ciphertext[offset : offset + max_nonfinal_len]
            offset += max_nonfinal_len
        else:
            chunk_ct = ciphertext[offset:]
            offset = len(ciphertext)
        if len(chunk_ct) < AEAD_TAG_SIZE:
            raise AgeFormatError("age payload chunk is shorter than the AEAD tag")
        try:
            chunk_pt = aead.decrypt(_chunk_nonce(chunk_index, final=final), chunk_ct, b"")
        except InvalidTag as exc:
            raise AgeDecryptError("age payload authentication failed") from exc
        if not final and len(chunk_pt) != CHUNK_SIZE:
            raise AgeFormatError("non-final age payload chunk decrypted to non-64KiB plaintext")
        if final and len(chunk_pt) == 0 and chunk_index != 0:
            raise AgeFormatError("empty final chunk is only valid for an empty payload")
        out.extend(chunk_pt)
        chunk_index += 1

    return bytes(out)


def iter_decrypt_payload_chunks(
    file_key: bytes,
    payload_nonce: bytes,
    ciphertext_chunks: Iterable[bytes],
) -> Iterator[bytes]:
    """Stream-decrypt age payload ciphertext after the 16-byte payload nonce."""

    if len(file_key) != FILE_KEY_SIZE:
        raise ValueError("file_key must be 16 bytes")
    if len(payload_nonce) != PAYLOAD_NONCE_SIZE:
        raise ValueError("payload_nonce must be 16 bytes")

    payload_key = _hkdf_sha256(file_key, salt=payload_nonce, info=PAYLOAD_HKDF_INFO)
    aead = ChaCha20Poly1305(payload_key)
    buffer = bytearray()
    chunk_index = 0
    max_nonfinal_len = CHUNK_SIZE + AEAD_TAG_SIZE

    for chunk in ciphertext_chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        while len(buffer) > max_nonfinal_len:
            chunk_ct = bytes(buffer[:max_nonfinal_len])
            del buffer[:max_nonfinal_len]
            try:
                chunk_pt = aead.decrypt(_chunk_nonce(chunk_index, final=False), chunk_ct, b"")
            except InvalidTag as exc:
                raise AgeDecryptError("age payload authentication failed") from exc
            if len(chunk_pt) != CHUNK_SIZE:
                raise AgeFormatError("non-final age payload chunk decrypted to non-64KiB plaintext")
            yield chunk_pt
            chunk_index += 1

    if len(buffer) < AEAD_TAG_SIZE:
        raise AgeFormatError("age payload must include at least one final chunk tag")
    try:
        chunk_pt = aead.decrypt(_chunk_nonce(chunk_index, final=True), bytes(buffer), b"")
    except InvalidTag as exc:
        raise AgeDecryptError("age payload authentication failed") from exc
    if len(chunk_pt) == 0 and chunk_index != 0:
        raise AgeFormatError("empty final chunk is only valid for an empty payload")
    yield chunk_pt


def split_plaintext_chunks(plaintext_size: int) -> list[tuple[int, int, int, bool]]:
    """
    Return `(chunk_index, start, end, final)` tuples for an age payload.

    Empty plaintext has exactly one final empty chunk, matching the age spec.
    """

    if plaintext_size < 0:
        raise ValueError("plaintext_size must be non-negative")
    count = age_chunk_count_for_plaintext_len(plaintext_size)
    result = []
    for idx in range(count):
        start = idx * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, plaintext_size)
        if plaintext_size == 0:
            start = end = 0
        result.append((idx, start, end, idx == count - 1))
    return result


def age_chunk_count_for_plaintext_len(plaintext_size: int) -> int:
    if plaintext_size < 0:
        raise ValueError("plaintext_size must be non-negative")
    if plaintext_size == 0:
        return 1
    return math.ceil(plaintext_size / CHUNK_SIZE)


def age_ciphertext_len_for_plaintext_len(plaintext_size: int, *, age_prefix_len: int) -> int:
    """Total binary .age object length for a given plaintext length."""

    if plaintext_size < 0:
        raise ValueError("plaintext_size must be non-negative")
    payload_chunks = split_plaintext_chunks(plaintext_size)
    payload_ciphertext = 0
    for _idx, start, end, _final in payload_chunks:
        payload_ciphertext += (end - start) + AEAD_TAG_SIZE
    return age_prefix_len + payload_ciphertext


def make_multipart_part_plans(
    plaintext_size: int,
    *,
    age_prefix_len: int,
    chunks_per_part: int = DEFAULT_CHUNKS_PER_MULTIPART_PART,
    enforce_portable_limits: bool = True,
) -> list[MultipartPartPlan]:
    """
    Build portable multipart plans aligned to age chunks.

    Part 1 includes the age header and 16-byte payload nonce, followed by a fixed
    group of encrypted age chunks. Later parts contain only encrypted chunks.
    """

    if plaintext_size < 0:
        raise ValueError("plaintext_size must be non-negative")
    if age_prefix_len < PAYLOAD_NONCE_SIZE:
        raise ValueError("age_prefix_len must include header and 16-byte payload nonce")
    if chunks_per_part <= 0:
        raise ValueError("chunks_per_part must be positive")

    chunk_count = age_chunk_count_for_plaintext_len(plaintext_size)
    plans: list[MultipartPartPlan] = []
    ciphertext_offset = 0
    part_number = 1
    for first_chunk in range(0, chunk_count, chunks_per_part):
        this_count = min(chunks_per_part, chunk_count - first_chunk)
        includes_prefix = first_chunk == 0
        plaintext_start = first_chunk * CHUNK_SIZE
        plaintext_end = min((first_chunk + this_count) * CHUNK_SIZE, plaintext_size)
        if plaintext_size == 0:
            plaintext_start = plaintext_end = 0
        # Each age chunk adds a 16-byte AEAD tag. The plaintext bytes covered
        # by this part are contiguous, so part length is prefix + plaintext span
        # + one tag per chunk. This avoids looping over every chunk for huge archives.
        part_len = (
            (age_prefix_len if includes_prefix else 0)
            + (plaintext_end - plaintext_start)
            + (this_count * AEAD_TAG_SIZE)
        )
        plans.append(
            MultipartPartPlan(
                part_number=part_number,
                first_chunk=first_chunk,
                chunk_count=this_count,
                includes_age_prefix=includes_prefix,
                plaintext_start=plaintext_start,
                plaintext_end=plaintext_end,
                ciphertext_start=ciphertext_offset,
                ciphertext_end=ciphertext_offset + part_len,
            )
        )
        ciphertext_offset += part_len
        part_number += 1
    if enforce_portable_limits:
        _validate_multipart_part_plans(plans)
    return plans


def _validate_multipart_part_plans(plans: Sequence[MultipartPartPlan]) -> None:
    if len(plans) > PORTABLE_MULTIPART_MAX_PARTS:
        raise ValueError(
            f"portable multipart construction supports at most {PORTABLE_MULTIPART_MAX_PARTS} "
            "parts; "
            f"plan has {len(plans)} parts. Increase chunks_per_part."
        )
    for plan in plans[:-1]:
        if plan.ciphertext_len < PORTABLE_MULTIPART_MIN_PART_BYTES:
            raise ValueError(
                f"multipart unit {plan.part_number} is below the 5 MiB portable minimum; "
                f"increase chunks_per_part or disable enforcement for tests"
            )
    for plan in plans:
        if plan.ciphertext_len > PORTABLE_MULTIPART_MAX_PART_BYTES:
            raise ValueError(
                f"multipart unit {plan.part_number} exceeds the 5 GiB portable maximum; "
                "decrease chunks_per_part"
            )


def parse_scrypt_header_from_age_file(age_file: bytes) -> ParsedScryptHeader:
    header_end = _age_header_end(age_file)
    if header_end is None:
        raise AgeFormatError("age header is missing MAC line")
    header = age_file[:header_end]
    return parse_scrypt_header(header)


def parse_scrypt_header(header: bytes) -> ParsedScryptHeader:
    if not header.startswith(AGE_V1_LINE):
        raise AgeFormatError("unsupported or missing age v1 version line")
    if not header.endswith(b"\n"):
        raise AgeFormatError("age header must end with a newline")

    lines = header.splitlines(keepends=True)
    if len(lines) < 4:
        raise AgeFormatError("age header is too short")
    if lines[0] != AGE_V1_LINE:
        raise AgeFormatError("unsupported age version line")
    arg_line = lines[1]
    if not arg_line.startswith(b"-> scrypt "):
        raise AgeFormatError("only one standard scrypt stanza is supported")
    args = arg_line.rstrip(b"\n").split(b" ")
    if len(args) != 4 or args[0] != b"->" or args[1] != b"scrypt":
        raise AgeFormatError("invalid scrypt stanza argument line")
    salt = _b64_decode(args[2].decode("ascii"))
    if len(salt) != SCRYPT_SALT_SIZE:
        raise AgeFormatError("scrypt salt must be 16 bytes")
    try:
        log_n_text = args[3].decode("ascii")
    except UnicodeDecodeError as exc:
        raise AgeFormatError("scrypt work factor is not ASCII") from exc
    if not log_n_text.isdecimal() or log_n_text.startswith("0"):
        raise AgeFormatError("invalid scrypt work factor encoding")
    log_n = int(log_n_text)
    try:
        _validate_log_n(log_n)
    except (TypeError, ValueError) as exc:
        raise AgeFormatError("invalid scrypt work factor") from exc

    mac_line_index = None
    for i, line in enumerate(lines[2:], start=2):
        if line.startswith(b"--- "):
            mac_line_index = i
            break
    if mac_line_index is None:
        raise AgeFormatError("age header is missing MAC line")
    if mac_line_index != len(lines) - 1:
        raise AgeFormatError("only a single scrypt stanza is supported")

    body_b64 = b"".join(line.rstrip(b"\n") for line in lines[2:mac_line_index])
    body = _b64_decode(body_b64.decode("ascii"))
    if len(body) != FILE_KEY_SIZE + AEAD_TAG_SIZE:
        raise AgeFormatError("scrypt stanza body must be 32 bytes")

    mac_line = lines[mac_line_index].rstrip(b"\n")
    if not mac_line.startswith(b"--- "):
        raise AgeFormatError("invalid age MAC line")
    mac = _b64_decode(mac_line[4:].decode("ascii"))
    if len(mac) != 32:
        raise AgeFormatError("age header MAC must be 32 bytes")
    mac_input = b"".join(lines[:mac_line_index]) + b"---"
    return ParsedScryptHeader(
        header=header,
        salt=salt,
        log_n=log_n,
        body=body,
        mac=mac,
        mac_input=mac_input,
    )


def _build_scrypt_header(salt: bytes, log_n: int, body: bytes, file_key: bytes) -> bytes:
    stanza = b"".join(
        [
            AGE_V1_LINE,
            b"-> scrypt "
            + _b64_encode(salt).encode("ascii")
            + b" "
            + str(log_n).encode("ascii")
            + b"\n",
            _wrap_b64_lines(body),
        ]
    )
    mac_input = stanza + b"---"
    hmac_key = _hkdf_sha256(file_key, salt=b"", info=HEADER_HKDF_INFO)
    mac = hmac.new(hmac_key, mac_input, hashlib.sha256).digest()
    return mac_input + b" " + _b64_encode(mac).encode("ascii") + b"\n"


def _unwrap_scrypt_file_key(
    passphrase: bytes,
    parsed: ParsedScryptHeader,
    *,
    scrypt_maxmem: int | None = None,
) -> bytes:
    wrap_key = _derive_scrypt_wrap_key(passphrase, parsed.salt, parsed.log_n, maxmem=scrypt_maxmem)
    try:
        file_key = ChaCha20Poly1305(wrap_key).decrypt(ZERO_AEAD_NONCE, parsed.body, b"")
    except InvalidTag as exc:
        raise AgeDecryptError("wrong passphrase or invalid scrypt recipient body") from exc
    if len(file_key) != FILE_KEY_SIZE:
        raise AgeDecryptError("invalid unwrapped age file key length")
    hmac_key = _hkdf_sha256(file_key, salt=b"", info=HEADER_HKDF_INFO)
    expected_mac = hmac.new(hmac_key, parsed.mac_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_mac, parsed.mac):
        raise AgeDecryptError("age header MAC verification failed")
    return file_key


def _derive_scrypt_wrap_key(
    passphrase: bytes, salt: bytes, log_n: int, *, maxmem: int | None
) -> bytes:
    _validate_log_n(log_n)
    if len(salt) != SCRYPT_SALT_SIZE:
        raise ValueError("scrypt salt must be 16 bytes")
    # OpenSSL-backed hashlib.scrypt has a conservative default memory cap. The
    # age default logN=18 needs about 256 MiB, so give it explicit headroom.
    if maxmem is None:
        maxmem = max(64 * 1024 * 1024, (128 * 8 * (1 << log_n)) * 2)
    return hashlib.scrypt(
        passphrase,
        salt=SCRYPT_SALT_PREFIX + salt,
        n=1 << log_n,
        r=8,
        p=1,
        dklen=32,
        maxmem=maxmem,
    )


def _validate_log_n(log_n: int) -> None:
    if not isinstance(log_n, int):
        raise TypeError("log_n must be an int")
    if log_n < 1 or log_n > 22:
        raise ValueError("scrypt log_n must be in the conservative range 1..22")


def _age_header_has_no_scrypt_recipient(age_file: bytes) -> bool:
    header_end = _age_header_end(age_file)
    if header_end is None:
        return False
    header = age_file[:header_end]
    lines = header.splitlines()
    if not lines or lines[0] != AGE_V1_LINE.rstrip(b"\n"):
        return False
    recipient_types = []
    for line in lines[1:]:
        if line.startswith(b"--- "):
            break
        if line.startswith(b"-> "):
            parts = line.split(b" ")
            if len(parts) >= 2:
                recipient_types.append(parts[1])
    return bool(recipient_types) and b"scrypt" not in recipient_types


def _age_header_end(data: bytes | bytearray) -> int | None:
    marker = b"\n--- "
    mac_line_start = data.find(marker)
    if mac_line_start < 0:
        return None
    mac_line_start += 1
    header_end = data.find(b"\n", mac_line_start)
    if header_end < 0:
        return None
    return header_end + 1


def _prepend(prefix: bytearray, source: Iterator[bytes]) -> Iterator[bytes]:
    if prefix:
        yield bytes(prefix)
    yield from source


def _hkdf_sha256(ikm: bytes, *, salt: bytes, info: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def _chunk_nonce(chunk_index: int, *, final: bool) -> bytes:
    return chunk_index.to_bytes(11, "big") + (b"\x01" if final else b"\x00")


def _passphrase_to_bytes(passphrase: str | bytes) -> bytes:
    if isinstance(passphrase, str):
        return passphrase.encode("utf-8")
    if isinstance(passphrase, bytes):
        return passphrase
    raise TypeError("passphrase must be str or bytes")


def _b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    if "=" in text:
        raise AgeFormatError("base64 padding is not permitted in age headers")
    try:
        raw = base64.b64decode(text + "=" * ((4 - len(text) % 4) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AgeFormatError("invalid base64 in age header") from exc
    if _b64_encode(raw) != text:
        raise AgeFormatError("non-canonical base64 in age header")
    return raw


def _wrap_b64_lines(data: bytes) -> bytes:
    text = _b64_encode(data).encode("ascii")
    if not text:
        return b"\n"
    return b"".join(text[i : i + 64] + b"\n" for i in range(0, len(text), 64))
