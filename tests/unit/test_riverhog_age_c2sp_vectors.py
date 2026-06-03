from __future__ import annotations

import hashlib
import os
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from riverhog_age import (
    PAYLOAD_NONCE_SIZE,
    AgeDecryptError,
    AgeFormatError,
    ResumableAgeScryptSession,
    decrypt_age_scrypt,
    parse_scrypt_header_from_age_file,
    split_plaintext_chunks,
)
from riverhog_age.resumable_age import decrypt_payload_chunks


def _testdata_root() -> Path:
    configured = os.environ.get("CCTV_AGE_TESTDATA")
    if not configured:
        pytest.skip("set CCTV_AGE_TESTDATA to run the C2SP/CCTV age vector suite")
    root = Path(configured)
    if not root.is_dir():
        pytest.fail(f"CCTV_AGE_TESTDATA does not exist or is not a directory: {root}")
    return root


def _parse_vector(path: Path) -> tuple[dict[str, list[str]], bytes]:
    data = path.read_bytes()
    raw_header, separator, body = data.partition(b"\n\n")
    if not separator:
        pytest.fail(f"{path.name}: missing C2SP vector separator")
    fields: dict[str, list[str]] = defaultdict(list)
    for line in raw_header.decode("utf-8").splitlines():
        key, value = line.split(": ", 1)
        fields[key].append(value)
    if fields.get("compressed") == ["zlib"]:
        body = zlib.decompress(body)
    return dict(fields), body


def _payload_region(age_file: bytes) -> tuple[bytes, bytes, bytes]:
    marker = b"\n--- "
    mac_line_start = age_file.find(marker)
    if mac_line_start < 0:
        raise AgeFormatError("missing MAC line")
    mac_line_start += 1
    header_end = age_file.find(b"\n", mac_line_start)
    if header_end < 0:
        raise AgeFormatError("missing header newline")
    header = age_file[: header_end + 1]
    payload_offset = len(header)
    nonce = age_file[payload_offset : payload_offset + PAYLOAD_NONCE_SIZE]
    ciphertext = age_file[payload_offset + PAYLOAD_NONCE_SIZE :]
    if len(nonce) != PAYLOAD_NONCE_SIZE:
        raise AgeFormatError("missing payload nonce")
    return header, nonce, ciphertext


def _first_recipient_type(age_file: bytes) -> bytes:
    header, _nonce, _ciphertext = _payload_region(age_file)
    for line in header.splitlines():
        if line.startswith(b"-> "):
            parts = line.split(b" ")
            return parts[1] if len(parts) > 1 else b""
    return b""


def test_c2sp_age_vectors_for_supported_surface():
    counts: Counter[str] = Counter()
    failures: list[tuple[str, str, str, str]] = []

    for path in sorted(_testdata_root().iterdir()):
        fields, age_file = _parse_vector(path)
        expect = fields["expect"][0]
        armored = fields.get("armored") == ["yes"]
        has_passphrase = "passphrase" in fields
        has_identity = "identity" in fields

        if has_passphrase and not has_identity and not armored:
            _check_passphrase_decrypt_vector(path, fields, age_file, expect, failures, counts)

        if expect == "success" and not armored and "file key" in fields:
            _check_stream_round_trip_vector(path, fields, age_file, failures, counts)

        if has_passphrase and not has_identity and not armored:
            _check_scrypt_header_parse_vector(path, age_file, expect, failures, counts)

    assert not failures
    assert counts["passphrase decrypt vectors ok"] > 0
    assert counts["stream round trips ok"] > 0
    assert counts["scrypt header parse vectors ok"] > 0


def _check_passphrase_decrypt_vector(
    path: Path,
    fields: dict[str, list[str]],
    age_file: bytes,
    expect: str,
    failures: list[tuple[str, str, str, str]],
    counts: Counter[str],
) -> None:
    passphrase = fields["passphrase"][0]
    try:
        plaintext = decrypt_age_scrypt(age_file, passphrase)
        got = "success"
    except AgeFormatError:
        got = "header failure"
        plaintext = b""
    except AgeDecryptError:
        got = "no match"
        plaintext = b""
    except Exception as exc:
        got = f"unexpected {type(exc).__name__}"
        plaintext = b""

    expected_bucket = "no match" if expect == "HMAC failure" else expect
    if got != expected_bucket:
        failures.append((path.name, "passphrase decrypt", expect, got))
        return
    if expect == "success":
        got_hash = hashlib.sha256(plaintext).hexdigest()
        if got_hash != fields["payload"][0]:
            failures.append((path.name, "passphrase payload hash", fields["payload"][0], got_hash))
            return
    counts["passphrase decrypt vectors ok"] += 1


def _check_stream_round_trip_vector(
    path: Path,
    fields: dict[str, list[str]],
    age_file: bytes,
    failures: list[tuple[str, str, str, str]],
    counts: Counter[str],
) -> None:
    try:
        header, nonce, ciphertext = _payload_region(age_file)
        file_key = bytes.fromhex(fields["file key"][0])
        plaintext = decrypt_payload_chunks(file_key, nonce, ciphertext)
        got_hash = hashlib.sha256(plaintext).hexdigest()
        if got_hash != fields["payload"][0]:
            failures.append((path.name, "stream payload hash", fields["payload"][0], got_hash))
            return
        session = ResumableAgeScryptSession(header=header, payload_nonce=nonce, file_key=file_key)
        rebuilt = b"".join(
            session.encrypt_chunk(idx, plaintext[start:end], final=final)
            for idx, start, end, final in split_plaintext_chunks(len(plaintext))
        )
        if rebuilt != ciphertext:
            failures.append((path.name, "stream round trip ciphertext", "exact match", "mismatch"))
            return
    except Exception as exc:
        failures.append((path.name, "stream exception", "success", f"{type(exc).__name__}: {exc}"))
        return
    counts["stream round trips ok"] += 1


def _check_scrypt_header_parse_vector(
    path: Path,
    age_file: bytes,
    expect: str,
    failures: list[tuple[str, str, str, str]],
    counts: Counter[str],
) -> None:
    try:
        recipient_type = _first_recipient_type(age_file)
    except AgeFormatError:
        recipient_type = b""
    if recipient_type != b"scrypt":
        return

    try:
        parse_scrypt_header_from_age_file(age_file)
        got = "parsed"
    except AgeFormatError:
        got = "header failure"
    except Exception as exc:
        got = f"unexpected {type(exc).__name__}"

    if expect == "header failure":
        if got != "header failure":
            failures.append((path.name, "scrypt parse", expect, got))
    elif got == "header failure":
        failures.append((path.name, "scrypt parse", expect, got))
    else:
        counts["scrypt header parse vectors ok"] += 1
