import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from riverhog_age import (
    AEAD_TAG_SIZE,
    CHUNK_SIZE,
    S3_MAX_PARTS,
    AgeDecryptError,
    AgeFormatError,
    ResumableAgeScryptSession,
    UploadState,
    age_ciphertext_len_for_plaintext_len,
    decrypt_age_scrypt,
    encrypt_age_scrypt,
    iter_decrypt_age_scrypt,
    parse_scrypt_header,
    parse_scrypt_header_from_age_file,
)
from riverhog_core.collection_archives import (
    CollectionArchiveExpectedFile,
    build_collection_archive_package_from_chunk_reader,
)

PASS = b"correct horse battery staple"
FAST_LOG_N = 14
FILE_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
SCRYPT_SALT = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
PAYLOAD_NONCE = bytes.fromhex("202122232425262728292a2b2c2d2e2f")
OFFICIAL_AGE = shutil.which("age")
OFFICIAL_BATCHPASS = shutil.which("age-plugin-batchpass")
official_age = pytest.mark.skipif(
    not OFFICIAL_AGE or not OFFICIAL_BATCHPASS,
    reason="official age and age-plugin-batchpass are required",
)


class FixtureProofStamper:
    def stamp(self, manifest_path):
        proof_path = manifest_path.with_name(f"{manifest_path.name}.ots")
        proof_path.write_bytes(b"fixture ots proof\n" + manifest_path.read_bytes()[:64])
        return proof_path


def new_test_session(plaintext_size=None):
    return ResumableAgeScryptSession.create(
        PASS,
        log_n=FAST_LOG_N,
        file_key=FILE_KEY,
        scrypt_salt=SCRYPT_SALT,
        payload_nonce=PAYLOAD_NONCE,
        plaintext_size=plaintext_size,
    )


@pytest.mark.parametrize(
    "size",
    [0, 1, 31, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE + 123],
)
def test_round_trip_for_relevant_sizes(size):
    plaintext = bytes((i * 131 + 7) % 256 for i in range(size))
    session = new_test_session(size)
    age_file = session.encrypt_plaintext(plaintext)

    assert age_file.startswith(b"age-encryption.org/v1\n-> scrypt ")
    assert decrypt_age_scrypt(age_file, PASS) == plaintext
    assert len(age_file) == age_ciphertext_len_for_plaintext_len(
        size, age_prefix_len=len(session.age_prefix)
    )


@official_age
@pytest.mark.parametrize(
    "size",
    [0, 1, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE + 17],
)
def test_module_output_decrypts_with_official_age_batchpass(tmp_path, size):
    plaintext = bytes((i * 17 + 23) % 256 for i in range(size))
    age_file = encrypt_age_scrypt(plaintext, PASS, log_n=FAST_LOG_N)
    age_path = tmp_path / "module.age"
    out_path = tmp_path / "module.dec"
    age_path.write_bytes(age_file)

    env = {
        **os.environ,
        "AGE_PASSPHRASE": PASS.decode("utf-8"),
        "AGE_PASSPHRASE_MAX_WORK_FACTOR": str(FAST_LOG_N),
    }
    subprocess.run(
        [OFFICIAL_AGE, "-d", "-j", "batchpass", "-o", str(out_path), str(age_path)],
        check=True,
        env=env,
        capture_output=True,
    )

    assert out_path.read_bytes() == plaintext


@official_age
@pytest.mark.parametrize(
    "size",
    [0, 1, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, 2 * CHUNK_SIZE + 17],
)
def test_official_age_batchpass_output_decrypts_with_module(tmp_path, size):
    plaintext = bytes((i * 19 + 29) % 256 for i in range(size))
    plain_path = tmp_path / "plain.bin"
    age_path = tmp_path / "official.age"
    plain_path.write_bytes(plaintext)

    env = {
        **os.environ,
        "AGE_PASSPHRASE": PASS.decode("utf-8"),
        "AGE_PASSPHRASE_WORK_FACTOR": str(FAST_LOG_N),
    }
    subprocess.run(
        [OFFICIAL_AGE, "-e", "-j", "batchpass", "-o", str(age_path), str(plain_path)],
        check=True,
        env=env,
        capture_output=True,
    )

    assert decrypt_age_scrypt(age_path.read_bytes(), PASS) == plaintext


@official_age
def test_production_work_factor_interop_with_official_age_batchpass(tmp_path):
    plaintext = (b"production-log-n-18-smoke\n" * 128) + bytes(range(256))
    passphrase = "riverhog production-shape smoke passphrase"
    module_age_path = tmp_path / "module-log18.age"
    module_dec_path = tmp_path / "module-log18.dec"
    official_plain_path = tmp_path / "official-log18.plain"
    official_age_path = tmp_path / "official-log18.age"

    module_age_path.write_bytes(encrypt_age_scrypt(plaintext, passphrase, log_n=18))
    env = {
        **os.environ,
        "AGE_PASSPHRASE": passphrase,
        "AGE_PASSPHRASE_MAX_WORK_FACTOR": "18",
    }
    subprocess.run(
        [OFFICIAL_AGE, "-d", "-j", "batchpass", "-o", str(module_dec_path), str(module_age_path)],
        check=True,
        env=env,
        capture_output=True,
    )
    assert module_dec_path.read_bytes() == plaintext

    official_plain_path.write_bytes(plaintext)
    env = {
        **os.environ,
        "AGE_PASSPHRASE": passphrase,
        "AGE_PASSPHRASE_WORK_FACTOR": "18",
    }
    subprocess.run(
        [
            OFFICIAL_AGE,
            "-e",
            "-j",
            "batchpass",
            "-o",
            str(official_age_path),
            str(official_plain_path),
        ],
        check=True,
        env=env,
        capture_output=True,
    )

    assert decrypt_age_scrypt(official_age_path.read_bytes(), passphrase) == plaintext


def test_deterministic_regeneration_after_export_import_state():
    plaintext = os.urandom(CHUNK_SIZE * 3 + 333)
    session1 = new_test_session(len(plaintext))
    state = session1.export_state(plaintext_size=len(plaintext)).to_json_bytes()

    session2 = ResumableAgeScryptSession.from_state(PASS, UploadState.from_json_bytes(state))

    assert session2.header == session1.header
    assert session2.payload_nonce == session1.payload_nonce
    assert session2.encrypt_plaintext(plaintext) == session1.encrypt_plaintext(plaintext)

    chunk_index = 2
    start = chunk_index * CHUNK_SIZE
    end = start + CHUNK_SIZE
    assert session2.encrypt_chunk(
        chunk_index, plaintext[start:end], final=False
    ) == session1.encrypt_chunk(chunk_index, plaintext[start:end], final=False)


def test_s3_part_plans_reconstruct_exact_age_file():
    plaintext = os.urandom(CHUNK_SIZE * 7 + 9)
    session = new_test_session(len(plaintext))
    full = session.encrypt_plaintext(plaintext)

    # Use small part grouping so this test exercises multiple parts without huge fixtures.
    plans = session.s3_part_plans(len(plaintext), chunks_per_part=3, enforce_s3_limits=False)
    assert len(plans) == 3
    assert plans[0].includes_age_prefix is True
    assert all(not p.includes_age_prefix for p in plans[1:])
    assert [p.part_number for p in plans] == [1, 2, 3]
    assert plans[0].ciphertext_start == 0
    assert plans[-1].ciphertext_end == len(full)

    def provider(_chunk_index, start, end):
        return plaintext[start:end]

    rebuilt = b"".join(
        session.encrypt_part(p, provider, plaintext_size=len(plaintext)) for p in plans
    )
    assert rebuilt == full
    assert decrypt_age_scrypt(rebuilt, PASS) == plaintext


def test_resume_from_middle_missing_part():
    plaintext = os.urandom(CHUNK_SIZE * 9 + 321)
    original = new_test_session(len(plaintext))
    state = original.export_state(plaintext_size=len(plaintext)).to_json_bytes()
    plans = original.s3_part_plans(len(plaintext), chunks_per_part=2, enforce_s3_limits=False)

    def provider(_chunk_index, start, end):
        return plaintext[start:end]

    uploaded_parts = {
        p.part_number: original.encrypt_part(p, provider, plaintext_size=len(plaintext))
        for p in plans
        if p.part_number != 3
    }

    resumed = ResumableAgeScryptSession.from_state(PASS, state)
    missing_plan = plans[2]
    uploaded_parts[3] = resumed.encrypt_part(missing_plan, provider, plaintext_size=len(plaintext))

    reconstructed = b"".join(uploaded_parts[p.part_number] for p in plans)
    assert reconstructed == original.encrypt_plaintext(plaintext)


def test_wrong_passphrase_fails():
    age_file = new_test_session().encrypt_plaintext(b"secret")
    with pytest.raises(AgeDecryptError):
        decrypt_age_scrypt(age_file, b"wrong passphrase")


def test_unsupported_recipient_type_is_no_match_for_decrypt():
    age_file = new_test_session().encrypt_plaintext(b"secret").replace(
        b"-> scrypt ", b"-> Scrypt ", 1
    )
    with pytest.raises(AgeDecryptError, match="no supported scrypt recipient"):
        decrypt_age_scrypt(age_file, PASS)


def test_payload_tamper_fails():
    age_file = bytearray(new_test_session().encrypt_plaintext(b"secret" * 1000))
    age_file[-1] ^= 0x01
    with pytest.raises(AgeDecryptError):
        decrypt_age_scrypt(bytes(age_file), PASS)


def test_header_tamper_fails():
    age_file = bytearray(new_test_session().encrypt_plaintext(b"secret"))
    # Flip a bit in the scrypt recipient body, not merely whitespace.
    marker = age_file.find(b"\n--- ")
    assert marker > 0
    age_file[marker - 2] = ord(b"A") if age_file[marker - 2] != ord(b"A") else ord(b"B")
    with pytest.raises((AgeDecryptError, Exception)):
        decrypt_age_scrypt(bytes(age_file), PASS)


def test_generated_header_parses_as_single_scrypt_stanza():
    age_file = new_test_session().encrypt_plaintext(b"abc")
    parsed = parse_scrypt_header_from_age_file(age_file)
    assert parsed.log_n == FAST_LOG_N
    assert parsed.salt == SCRYPT_SALT
    assert len(parsed.body) == 16 + AEAD_TAG_SIZE
    assert len(parsed.mac) == 32


def test_small_file_convenience_encryptor():
    plaintext = b'{"collection":"example"}'
    age_file = encrypt_age_scrypt(plaintext, PASS, log_n=FAST_LOG_N)
    assert decrypt_age_scrypt(age_file, PASS) == plaintext


@pytest.mark.parametrize(
    "size",
    [0, 1, CHUNK_SIZE - 1, CHUNK_SIZE, CHUNK_SIZE + 1, CHUNK_SIZE * 3 + 111],
)
def test_streaming_decrypt_handles_arbitrary_transport_chunking(size):
    plaintext = bytes((i * 23 + 5) % 256 for i in range(size))
    age_file = encrypt_age_scrypt(plaintext, PASS, log_n=FAST_LOG_N)
    chunks = [age_file[offset : offset + 997] for offset in range(0, len(age_file), 997)]

    assert b"".join(iter_decrypt_age_scrypt(chunks, PASS)) == plaintext


@official_age
def test_streaming_decrypt_handles_official_age_batchpass_output(tmp_path):
    plaintext = bytes((i * 31 + 13) % 256 for i in range(CHUNK_SIZE * 2 + 777))
    plain_path = tmp_path / "plain.bin"
    age_path = tmp_path / "official.age"
    plain_path.write_bytes(plaintext)
    env = {
        **os.environ,
        "AGE_PASSPHRASE": PASS.decode("utf-8"),
        "AGE_PASSPHRASE_WORK_FACTOR": str(FAST_LOG_N),
    }
    subprocess.run(
        [OFFICIAL_AGE, "-e", "-j", "batchpass", "-o", str(age_path), str(plain_path)],
        check=True,
        env=env,
        capture_output=True,
    )
    age_file = age_path.read_bytes()
    chunks = [age_file[offset : offset + 1234] for offset in range(0, len(age_file), 1234)]

    assert b"".join(iter_decrypt_age_scrypt(chunks, PASS)) == plaintext


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (
            lambda header: header.replace(
                b"age-encryption.org/v1\n", b"age-encryption.org/v2\n", 1
            ),
            "version",
        ),
        (lambda header: header.replace(b"-> scrypt ", b"-> X25519 ", 1), "scrypt"),
        (lambda header: header.replace(b" 14\n", b" 014\n", 1), "work factor"),
        (lambda header: header.replace(b" 14\n", b" 23\n", 1), "work factor"),
        (
            lambda header: header.replace(b" 14\n", b" 9223372036854775818\n", 1),
            "work factor",
        ),
        (lambda header: header.replace(b"--- ", b"--- A===\n", 1), "single scrypt"),
        (lambda header: header.rstrip(b"\n"), "newline"),
    ],
)
def test_malformed_age_headers_fail_cleanly(mutator, match):
    header = parse_scrypt_header_from_age_file(new_test_session().encrypt_plaintext(b"abc")).header
    with pytest.raises(AgeFormatError, match=match):
        parse_scrypt_header(mutator(header))


def test_upload_state_parser_rejects_malformed_state():
    state = new_test_session().export_state(plaintext_size=123).to_json_bytes()
    payload = state.decode("utf-8").replace("age-v1-scrypt-resumable", "future-format")

    with pytest.raises(AgeFormatError, match="unsupported"):
        UploadState.from_json_bytes(payload)

    bad_nonce = {
        "format": "age-v1-scrypt-resumable",
        "header_b64": "AA",
        "payload_nonce_b64": "AA",
        "plaintext_size": 123,
    }
    with pytest.raises(AgeFormatError, match="payload nonce"):
        UploadState.from_json_bytes(json.dumps(bad_nonce).encode("utf-8"))


def test_default_s3_plan_handles_70_gib_archive_shape():
    session = new_test_session()
    seventy_gib = 70 * 1024**3
    plans = session.s3_part_plans(seventy_gib)
    assert len(plans) < 10_000
    assert all(p.ciphertext_len >= 5 * 1024 * 1024 for p in plans[:-1])
    assert plans[-1].ciphertext_end == age_ciphertext_len_for_plaintext_len(
        seventy_gib, age_prefix_len=len(session.age_prefix)
    )


def test_s3_plan_rejects_too_many_small_parts_when_enforced():
    session = new_test_session()
    with pytest.raises(ValueError, match="Increase chunks_per_part|below the 5 MiB"):
        session.s3_part_plans(70 * 1024**3, chunks_per_part=1)


def test_s3_plan_accepts_exactly_max_parts_and_rejects_one_more():
    session = new_test_session()
    max_size = S3_MAX_PARTS * CHUNK_SIZE
    plans = session.s3_part_plans(max_size, chunks_per_part=1, enforce_s3_limits=False)
    assert len(plans) == S3_MAX_PARTS

    with pytest.raises(ValueError, match="at most"):
        session.s3_part_plans(max_size + CHUNK_SIZE, chunks_per_part=1)


def test_riverhog_tar_range_provider_reconstructs_exact_age_file():
    files_by_path = {
        "clips/large.mov": bytes((i * 7 + 11) % 256 for i in range(CHUNK_SIZE * 2 + 711)),
        "docs/readme.txt": b"hello from a tiny file\n",
        "empty.bin": b"",
    }
    expected_files = tuple(
        CollectionArchiveExpectedFile(
            path=path,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, content in files_by_path.items()
    )

    def read_chunks(path):
        content = files_by_path[path]
        for offset in range(0, len(content), 8191):
            yield content[offset : offset + 8191]

    def read_range(path, offset, size):
        content = files_by_path[path]
        end = None if size is None else offset + size
        for chunk_offset in range(offset, len(content) if end is None else end, 4093):
            range_end = len(content) if end is None else end
            yield content[chunk_offset : min(chunk_offset + 4093, range_end)]

    package = build_collection_archive_package_from_chunk_reader(
        collection_id="2026/20260601T120000Z__range-test",
        files=expected_files,
        read_file_chunks=read_chunks,
        read_file_chunks_range=read_range,
        stamper=FixtureProofStamper(),
    )
    plaintext = package.archive_bytes
    session = new_test_session(len(plaintext))
    full = session.encrypt_plaintext(plaintext)
    plans = session.s3_part_plans(len(plaintext), chunks_per_part=2, enforce_s3_limits=False)

    def provider(_chunk_index, start, end):
        return b"".join(package.iter_archive_from_offset(start))[: end - start]

    rebuilt = b"".join(
        session.encrypt_part(plan, provider, plaintext_size=len(plaintext)) for plan in plans
    )
    assert rebuilt == full
    assert decrypt_age_scrypt(rebuilt, PASS) == plaintext

    with tarfile.open(fileobj=BytesIO(decrypt_age_scrypt(rebuilt, PASS)), mode="r:") as archive:
        assert sorted(archive.getnames()) == sorted(files_by_path)
