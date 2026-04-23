from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

CHUNK_SIZE = 1024 * 1024
AGE_STREAM_CHUNK_SIZE = 64 * 1024
AGE_STREAM_TAG_BYTES = 16
AGE_MAGIC_PREFIXES = (
    b"age-encryption.org/",
    b"-----BEGIN AGE ENCRYPTED FILE-----",
)


class AgeEncryptionError(RuntimeError):
    pass


def _read_head(path: Path, size: int = 64) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def is_age_encrypted_file(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    head = _read_head(path)
    return any(head.startswith(prefix) for prefix in AGE_MAGIC_PREFIXES)


def encrypted_size_for_plaintext_size(plaintext_size: int) -> int:
    if plaintext_size < 0:
        raise ValueError("plaintext_size must be non-negative")
    chunks = math.ceil(plaintext_size / AGE_STREAM_CHUNK_SIZE)
    return plaintext_size + chunks * AGE_STREAM_TAG_BYTES


def max_plaintext_size_for_encrypted_budget(budget: int) -> int:
    if budget < 0:
        raise ValueError("budget must be non-negative")
    low, high = 0, budget
    while low < high:
        mid = (low + high + 1) // 2
        if encrypted_size_for_plaintext_size(mid) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


def _iter_file_chunks(path: Path, *, offset: int = 0, size: int | None = None) -> Iterator[bytes]:
    remaining = size
    with path.open("rb") as handle:
        if offset:
            handle.seek(offset)
        while True:
            if remaining is not None and remaining <= 0:
                break
            chunk = handle.read(CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def _run_age_encrypt(
    plaintext: Iterator[bytes], dest: Path, *, age_cli: str = "age", passphrase: str | None = None
) -> None:
    env = os.environ.copy()
    if passphrase is not None:
        env["AGE_PASSPHRASE"] = passphrase
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [age_cli, "-e", "-p", "-o", str(dest)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None
    try:
        for chunk in plaintext:
            proc.stdin.write(chunk)
        proc.stdin.close()
        _stdout, stderr = proc.communicate()
    except Exception:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise AgeEncryptionError(
            stderr.decode("utf-8", errors="replace") or "age encryption failed"
        )


def encrypt_bytes_to_file(
    data: bytes, dest: Path, *, age_cli: str = "age", passphrase: str | None = None
) -> None:
    _run_age_encrypt(iter([data]), dest, age_cli=age_cli, passphrase=passphrase)


def encrypt_file_span(
    source: Path,
    dest: Path,
    offset: int = 0,
    size: int | None = None,
    *,
    age_cli: str = "age",
    passphrase: str | None = None,
) -> None:
    _run_age_encrypt(
        _iter_file_chunks(source, offset=offset, size=size),
        dest,
        age_cli=age_cli,
        passphrase=passphrase,
    )


def decrypt_file_to_bytes(
    path: Path, *, age_cli: str = "age", passphrase: str | None = None
) -> bytes:
    env = os.environ.copy()
    if passphrase is not None:
        env["AGE_PASSPHRASE"] = passphrase
    proc = subprocess.run(
        [age_cli, "-d", str(path)],
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AgeEncryptionError(
            proc.stderr.decode("utf-8", errors="replace") or "age decryption failed"
        )
    return proc.stdout


def decrypt_tree(
    source_root: Path, dest_root: Path, *, age_cli: str = "age", passphrase: str | None = None
) -> None:
    for path in sorted(candidate for candidate in source_root.rglob("*") if candidate.is_file()):
        rel = path.relative_to(source_root)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(decrypt_file_to_bytes(path, age_cli=age_cli, passphrase=passphrase))


def logical_file_sha256_and_size(
    path: Path, *, decrypt: bool = False, age_cli: str = "age", passphrase: str | None = None
) -> tuple[str, int]:
    if decrypt and is_age_encrypted_file(path):
        data = decrypt_file_to_bytes(path, age_cli=age_cli, passphrase=passphrase)
        return hashlib.sha256(data).hexdigest(), len(data)

    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


@lru_cache(maxsize=1)
def age_is_available(age_cli: str = "age") -> bool:
    return shutil.which(age_cli) is not None
