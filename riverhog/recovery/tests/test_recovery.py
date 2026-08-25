from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
import riverhog_recover.recovery as recovery_module
from riverhog_age import encrypt_age_scrypt
from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    ArchiveRootCiphertextIdentity,
    CollectionEncryptionBinding,
    RecoveryDescriptor,
)
from riverhog_core.archive_manifest import build_collection_archive_manifest
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedPackVolume,
    SealedProvenanceObject,
    SealedRawVolume,
    StoredArchivePart,
    VerifiedRawFile,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.raw_verification import raw_file_volume_set_sha256
from riverhog_provenance import (
    FileProvenanceBinding,
    build_portable_provenance_set,
    build_provenance_archive,
    create_derivative_journal_from_identity,
    create_observation_journal,
    prepare_file_provenance,
    validate_journal,
    validate_portable_provenance_set,
)
from riverhog_recover import RecoveryError, recover_archive

from tests.fixtures.archive import age_state_json

PASSPHRASE = "correct horse battery archive"
PASSPHRASE_ID = "recovery-test-key-v1"
OFFICIAL_AGE = shutil.which("age")
OFFICIAL_BATCHPASS = shutil.which("age-plugin-batchpass")

pytestmark = pytest.mark.skipif(
    OFFICIAL_AGE is None or OFFICIAL_BATCHPASS is None,
    reason="official age and age-plugin-batchpass are required",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file(path: str, content: bytes) -> ArchiveFile:
    return ArchiveFile(path=path, bytes=len(content), sha256=_sha256(content))


def _part(plaintext: bytes, ciphertext: bytes) -> tuple[StoredArchivePart, ...]:
    return (
        StoredArchivePart(
            number=1,
            plaintext_start=0,
            plaintext_bytes=len(plaintext),
            plaintext_sha256=_sha256(plaintext),
            stored_bytes=len(ciphertext),
            stored_sha256=_sha256(ciphertext),
        ),
    )


def _write_ots_command(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import sys
from pathlib import Path

if len(sys.argv) != 5 or sys.argv[1] != "verify" or sys.argv[3] != "-f":
    raise SystemExit(2)
proof = Path(sys.argv[2]).read_text(encoding="utf-8")
manifest = Path(sys.argv[4]).read_bytes()
expected = f"sha256:{hashlib.sha256(manifest).hexdigest()}\\n"
if proof != expected:
    print("proof does not match manifest", file=sys.stderr)
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _write_archive(
    root: Path,
    *,
    passphrase: str = PASSPHRASE,
    passphrase_id: str = PASSPHRASE_ID,
    with_provenance: bool = False,
    provenance_journal: bytes | None = None,
    provenance_journals: Mapping[str, bytes] | None = None,
) -> tuple[dict[str, bytes], bytes | None]:
    expected = {
        "notes/alpha.txt": b"alpha\n",
        "notes/beta.txt": b"beta\n",
        "video.bin": b"first-second",
    }
    files = tuple(_file(path, content) for path, content in sorted(expected.items()))

    pack_files = tuple(current for current in files if current.path.startswith("notes/"))
    pack_plan = plan_pack_volume(pack_files, sequence=0)
    pack_plaintext = b"".join(
        iter_render_pack_upload_unit(
            pack_plan,
            0,
            lambda path: (expected[path],),
        )
    )
    pack_ciphertext = encrypt_age_scrypt(pack_plaintext, passphrase, log_n=1)
    sealed_pack = SealedPackVolume(
        volume_id=pack_plan.volume_id,
        sequence=0,
        relative_path=f"volumes/{pack_plan.volume_id}.tar.age",
        files=len(pack_files),
        source_bytes=sum(current.bytes for current in pack_files),
        plaintext_bytes=len(pack_plaintext),
        age_state_json=age_state_json(len(pack_plaintext)),
        index_sha256=pack_plan.index_sha256,
        plan_sha256=pack_plan.plan_sha256,
        parts=_part(pack_plaintext, pack_ciphertext),
        revision="pack-version",
        completed_at="2026-08-08T00:00:00Z",
    )

    raw_file = next(current for current in files if current.path == "video.bin")
    raw_volumes: list[SealedRawVolume] = []
    raw_ciphertexts: dict[str, bytes] = {}
    offset = 0
    for sequence, plaintext in enumerate((b"first-", b"second"), start=1):
        volume_id = f"segment-{sequence:012d}"
        relative_path = f"volumes/{volume_id}.bin.age"
        ciphertext = encrypt_age_scrypt(plaintext, passphrase, log_n=1)
        raw_ciphertexts[relative_path] = ciphertext
        raw_volumes.append(
            SealedRawVolume(
                volume_id=volume_id,
                sequence=sequence,
                relative_path=relative_path,
                source_path=raw_file.path,
                file_offset=offset,
                plaintext_bytes=len(plaintext),
                file_bytes=raw_file.bytes,
                file_sha256=raw_file.sha256,
                age_state_json=age_state_json(len(plaintext)),
                parts=_part(plaintext, ciphertext),
                revision=f"segment-version-{sequence}",
                completed_at="2026-08-08T00:00:00Z",
            )
        )
        offset += len(plaintext)
    verified_raw = VerifiedRawFile(
        path=raw_file.path,
        bytes=raw_file.bytes,
        sha256=raw_file.sha256,
        volume_set_sha256=raw_file_volume_set_sha256(
            file=raw_file,
            volumes=raw_volumes,
        ),
        verified_at="2026-08-08T00:00:00Z",
    )
    provenance_identity: str | None = None
    provenance_objects: tuple[SealedProvenanceObject, ...] = ()
    provenance_ciphertexts: dict[str, bytes] = {}
    exact_journal: bytes | None = None
    if with_provenance:
        exact_journal = provenance_journal
        if exact_journal is None:
            observed = root.parent / "observed-alpha.txt"
            observed.write_bytes(expected["notes/alpha.txt"])
            exact_journal = create_observation_journal(
                observed,
                relative_path="notes/alpha.txt",
                host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
                agent_name="recovery-fixture",
                agent_version="1.0.0",
            )
            observed.unlink()
        summary = validate_journal(exact_journal)
        bindings = tuple(
            FileProvenanceBinding(
                path=current.path,
                bytes=current.bytes,
                sha256=current.sha256,
                status="captured" if current.path == "notes/alpha.txt" else "omitted",
                journal_id=(summary.journal_id if current.path == "notes/alpha.txt" else None),
                current_state_id=(
                    summary.current_state_id if current.path == "notes/alpha.txt" else None
                ),
                omission_reason=(
                    None
                    if current.path == "notes/alpha.txt"
                    else "fixture explicitly omitted source provenance"
                ),
            )
            for current in files
        )
        journal_set = dict(provenance_journals or {summary.journal_id: exact_journal})
        if journal_set.get(summary.journal_id) != exact_journal:
            raise ValueError("recovery fixture current journal is missing from its exact set")
        provenance = build_provenance_archive(
            bindings=bindings,
            journals=journal_set,
        )
        sealed: list[SealedProvenanceObject] = []
        for object_id, kind, relative_path, plaintext in (
            *(
                (
                    bundle.bundle_id,
                    "provenance-bundle",
                    bundle.relative_path,
                    bundle.content,
                )
                for bundle in provenance.bundles
            ),
            (
                "provenance-index",
                "provenance-index",
                "provenance/index.json.age",
                provenance.index_bytes,
            ),
        ):
            ciphertext = encrypt_age_scrypt(plaintext, passphrase, log_n=1)
            provenance_ciphertexts[relative_path] = ciphertext
            sealed.append(
                SealedProvenanceObject(
                    object_id=object_id,
                    kind=kind,
                    relative_path=relative_path,
                    plaintext_bytes=len(plaintext),
                    plaintext_sha256=_sha256(plaintext),
                    stored_bytes=len(ciphertext),
                    stored_sha256=_sha256(ciphertext),
                    revision=f"{object_id}-version",
                    completed_at="2026-08-08T00:00:00Z",
                )
            )
        provenance_identity = provenance.identity
        provenance_objects = tuple(sealed)

    manifest = build_collection_archive_manifest(
        files=files,
        packs=((pack_plan, sealed_pack),),
        raw_volumes=raw_volumes,
        verified_raw_files=(verified_raw,),
        provenance_identity=provenance_identity,
        provenance_objects=provenance_objects,
    )
    proof = f"sha256:{_sha256(manifest)}\n".encode()

    encrypted_manifest = encrypt_age_scrypt(manifest, passphrase, log_n=1)
    descriptor = RecoveryDescriptor(
        encryption=CollectionEncryptionBinding(
            format=ARCHIVE_ENCRYPTION_FORMAT,
            passphrase_id=passphrase_id,
        ),
        root=ArchiveRootCiphertextIdentity(
            path="manifest.json.age",
            stored_bytes=len(encrypted_manifest),
            stored_sha256=_sha256(encrypted_manifest),
        ),
    ).to_json_bytes()
    ciphertext: dict[str, bytes] = {
        "manifest.json.age": encrypted_manifest,
        "manifest.json.ots.age": encrypt_age_scrypt(proof, passphrase, log_n=1),
        "recovery.json": descriptor,
        sealed_pack.relative_path: pack_ciphertext,
        **raw_ciphertexts,
        **provenance_ciphertexts,
    }
    for relative, content in ciphertext.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (root / "SHA256SUMS").write_text(
        "".join(
            f"{_sha256(content)}  {relative}\n"
            for relative, content in sorted(ciphertext.items())
            if not relative.startswith("volumes/")
        ),
        encoding="utf-8",
    )
    return expected, exact_journal


def test_recovers_complete_collection_without_server_or_database(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    expected, _journal = _write_archive(archive)
    output = tmp_path / "recovered"
    ots = _write_ots_command(tmp_path / "ots-fixture")

    summary = recover_archive(
        archive,
        output,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
        ots_command=str(ots),
    )

    assert summary.files == len(expected)
    assert summary.bytes == sum(len(content) for content in expected.values())
    assert summary.volumes == 3
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_recovery_selects_exact_key_generations_without_trial_decryption(tmp_path: Path) -> None:
    second_id = "recovery-test-key-v2"
    second_passphrase = "second independent archive secret"
    first_archive = tmp_path / "archive-one"
    second_archive = tmp_path / "archive-two"
    expected, _journal = _write_archive(first_archive)
    _write_archive(
        second_archive,
        passphrase=second_passphrase,
        passphrase_id=second_id,
    )
    ots = _write_ots_command(tmp_path / "ots-fixture")
    passphrases = {
        PASSPHRASE_ID: PASSPHRASE,
        second_id: second_passphrase,
    }

    for archive, output in (
        (first_archive, tmp_path / "recovered-one"),
        (second_archive, tmp_path / "recovered-two"),
    ):
        recover_archive(
            archive,
            output,
            passphrases=passphrases,
            ots_command=str(ots),
        )
        assert {path: (output / path).read_bytes() for path in expected} == expected

    with pytest.raises(RecoveryError, match=second_id):
        recover_archive(
            second_archive,
            tmp_path / "missing-key-output",
            passphrases={PASSPHRASE_ID: PASSPHRASE},
            ots_command=str(ots),
        )


def test_cli_recovers_with_permission_restricted_passphrase_file(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    expected, _journal = _write_archive(archive)
    output = tmp_path / "recovered"
    passphrases_file = tmp_path / "passphrases.json"
    passphrases_file.write_text(
        f'{{"{PASSPHRASE_ID}":"{PASSPHRASE}"}}',
        encoding="utf-8",
    )
    passphrases_file.chmod(0o600)
    ots = _write_ots_command(tmp_path / "ots-fixture")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "riverhog_recover.cli",
            str(archive),
            str(output),
            "--passphrases-file",
            str(passphrases_file),
            "--ots-command",
            str(ots),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Recovered 3 files" in completed.stdout
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_windows_recovery_uses_batchpass_environment_without_unix_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.age"
    source.write_bytes(b"ciphertext")
    destination = tmp_path / "destination"

    def run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
        pass_fds: tuple[int, ...],
    ) -> subprocess.CompletedProcess[str]:
        assert command == [
            "age",
            "--decrypt",
            "-j",
            "batchpass",
            "-o",
            str(destination),
            str(source),
        ]
        assert check is False
        assert capture_output is True
        assert text is True
        assert env["AGE_PASSPHRASE"] == PASSPHRASE
        assert "AGE_PASSPHRASE_FD" not in env
        assert pass_fds == ()
        destination.write_bytes(b"plaintext")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(recovery_module, "_windows_host", lambda: True)
    monkeypatch.setattr(recovery_module.subprocess, "run", run)

    recovery_module._age_decrypt(
        source,
        destination,
        passphrase=PASSPHRASE,
        command="age",
    )

    assert destination.read_bytes() == b"plaintext"


def test_ciphertext_corruption_fails_without_publishing_partial_output(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    damaged = archive / "volumes/segment-000000000001.bin.age"
    damaged.write_bytes(damaged.read_bytes() + b"damage")
    output = tmp_path / "recovered"
    ots = _write_ots_command(tmp_path / "ots-fixture")

    with pytest.raises(RecoveryError, match="stored volume byte count mismatch"):
        recover_archive(
            archive,
            output,
            passphrases={PASSPHRASE_ID: PASSPHRASE},
            ots_command=str(ots),
        )

    assert not output.exists()


def test_recovery_descriptor_rejects_changed_root_before_decryption(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    (archive / "SHA256SUMS").unlink()
    root = archive / "manifest.json.age"
    root.write_bytes(root.read_bytes() + b"changed")

    with pytest.raises(RecoveryError, match="does not match recovery descriptor"):
        recover_archive(
            archive,
            tmp_path / "output",
            passphrases={PASSPHRASE_ID: PASSPHRASE},
            ots_command=str(_write_ots_command(tmp_path / "ots-fixture")),
        )


def test_client_transform_riverhog_recovery_restores_exact_derivative_history(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-alpha.txt"
    source.write_bytes(b"original alpha\n")
    client_journal = create_observation_journal(
        source,
        relative_path="camera/alpha.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="riverhog-client",
        agent_version="1.0.0",
    )
    transformed = b"alpha\n"
    target_journal = create_derivative_journal_from_identity(
        relative_path="notes/alpha.txt",
        byte_count=len(transformed),
        sha256=hashlib.sha256(transformed).hexdigest(),
        source_journals=(client_journal,),
        agent_name="target-server",
        agent_version="1.0.0",
        event_label="Target canonical archive transformation",
        started_at="2026-08-10T01:00:00Z",
        ended_at="2026-08-10T01:01:00Z",
    )
    client_summary = validate_journal(client_journal)
    target_summary = validate_journal(target_journal)
    journals = {
        client_summary.journal_id: client_journal,
        target_summary.journal_id: target_journal,
    }

    archive = tmp_path / "archive"
    expected, exact_journal = _write_archive(
        archive,
        with_provenance=True,
        provenance_journal=target_journal,
        provenance_journals=journals,
    )
    assert exact_journal is not None
    exact_summary = validate_journal(exact_journal)
    assert exact_summary.primary_lineage_id != client_summary.primary_lineage_id
    assert {item.journal_id for item in exact_summary.external_states} == {
        client_summary.journal_id
    }
    output = tmp_path / "recovered"
    ots = _write_ots_command(tmp_path / "ots-fixture")

    summary = recover_archive(
        archive,
        output,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
        ots_command=str(ots),
    )

    provenance_root = output / ".riverhog" / "provenance"
    index = (provenance_root / "index.json").read_bytes()
    restored_journals = {
        journal_id: (provenance_root / "journals" / f"{journal_id}.json-seq").read_bytes()
        for journal_id in journals
    }
    validated = validate_portable_provenance_set(index, restored_journals)
    assert summary.provenance_mode == "mixed"
    assert summary.provenance_journals == 2
    assert restored_journals == journals
    assert index == build_portable_provenance_set(
        bindings=validated.bindings,
        journals=restored_journals,
    )
    prepared = prepare_file_provenance(
        output / "notes" / "alpha.txt",
        relative_path="notes/alpha.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000002",
        agent_name="riverhog-client",
        agent_version="1.0.0",
        provenance=provenance_root,
    )
    assert prepared.journals == journals
    assert {path: (output / path).read_bytes() for path in expected} == expected
