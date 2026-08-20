from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import riverhog_recover.recovery as recovery_module
from riverhog_age import encrypt_age_scrypt
from riverhog_core.archive_manifest import build_collection_archive_manifest
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedPackVolume,
    SealedProvenanceObject,
    SealedRawVolume,
    StoredPartReceipt,
    VerifiedRawFile,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.raw_verification import raw_file_volume_set_sha256
from riverhog_provenance import (
    FileProvenanceBinding,
    append_observation,
    append_replacement_transformation,
    build_portable_provenance_set,
    build_provenance_archive,
    create_observation_journal,
    prepare_file_provenance,
    validate_journal,
    validate_portable_provenance_set,
)
from riverhog_recover import RecoveryError, recover_archive

from tests.fixtures.archive import age_state_json

PASSPHRASE = "correct horse battery archive"
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


def _part(plaintext: bytes, ciphertext: bytes) -> tuple[StoredPartReceipt, ...]:
    return (
        StoredPartReceipt(
            number=1,
            plaintext_start=0,
            plaintext_bytes=len(plaintext),
            plaintext_sha256=_sha256(plaintext),
            stored_bytes=len(ciphertext),
            stored_sha256=_sha256(ciphertext),
            etag="fixture-etag",
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
    with_provenance: bool = False,
    provenance_journal: bytes | None = None,
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
    pack_ciphertext = encrypt_age_scrypt(pack_plaintext, PASSPHRASE, log_n=1)
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
        version_id="pack-version",
        completed_at="2026-08-08T00:00:00Z",
    )

    raw_file = next(current for current in files if current.path == "video.bin")
    raw_volumes: list[SealedRawVolume] = []
    raw_ciphertexts: dict[str, bytes] = {}
    offset = 0
    for sequence, plaintext in enumerate((b"first-", b"second"), start=1):
        volume_id = f"segment-{sequence:012d}"
        relative_path = f"volumes/{volume_id}.bin.age"
        ciphertext = encrypt_age_scrypt(plaintext, PASSPHRASE, log_n=1)
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
                version_id=f"segment-version-{sequence}",
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
        provenance = build_provenance_archive(
            bindings=bindings,
            journals={summary.journal_id: exact_journal},
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
            ciphertext = encrypt_age_scrypt(plaintext, PASSPHRASE, log_n=1)
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
                    version_id=f"{object_id}-version",
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

    ciphertext: dict[str, bytes] = {
        "manifest.json.age": encrypt_age_scrypt(manifest, PASSPHRASE, log_n=1),
        "manifest.json.ots.age": encrypt_age_scrypt(proof, PASSPHRASE, log_n=1),
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
            f"{_sha256(content)}  {relative}\n" for relative, content in sorted(ciphertext.items())
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
        passphrase=PASSPHRASE,
        ots_command=str(ots),
    )

    assert summary.files == len(expected)
    assert summary.bytes == sum(len(content) for content in expected.values())
    assert summary.volumes == 3
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_cli_recovers_with_permission_restricted_passphrase_file(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    expected, _journal = _write_archive(archive)
    output = tmp_path / "recovered"
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text(PASSPHRASE, encoding="utf-8")
    passphrase_file.chmod(0o600)
    ots = _write_ots_command(tmp_path / "ots-fixture")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "riverhog_recover.cli",
            str(archive),
            str(output),
            "--passphrase-file",
            str(passphrase_file),
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

    with pytest.raises(RecoveryError, match="ciphertext checksum mismatch"):
        recover_archive(
            archive,
            output,
            passphrase=PASSPHRASE,
            ots_command=str(ots),
        )

    assert not output.exists()


def test_client_stove0_target_riverhog_recovery_preserves_one_exact_main_lineage(
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
    staged = tmp_path / "staged-alpha.txt"
    staged.write_bytes(source.read_bytes())
    stove0_journal = append_observation(
        client_journal,
        staged,
        relative_path="staging/alpha.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000002",
        agent_name="stove0-server",
        agent_version="1.0.0",
    )
    transformed = tmp_path / "transformed-alpha.txt"
    transformed.write_bytes(b"alpha\n")
    target_journal = append_replacement_transformation(
        stove0_journal,
        transformed,
        relative_path="notes/alpha.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000003",
        agent_name="target-server",
        agent_version="1.0.0",
        event_label="Target canonical archive transformation",
        started_at="2026-08-10T01:00:00Z",
        ended_at="2026-08-10T01:01:00Z",
    )
    assert stove0_journal.startswith(client_journal)
    assert target_journal.startswith(stove0_journal)

    archive = tmp_path / "archive"
    expected, exact_journal = _write_archive(
        archive,
        with_provenance=True,
        provenance_journal=target_journal,
    )
    assert exact_journal is not None
    exact_summary = validate_journal(exact_journal)
    assert exact_summary.primary_lineage_id == validate_journal(client_journal).primary_lineage_id
    output = tmp_path / "recovered"
    ots = _write_ots_command(tmp_path / "ots-fixture")

    summary = recover_archive(
        archive,
        output,
        passphrase=PASSPHRASE,
        ots_command=str(ots),
    )

    provenance_root = output / ".riverhog" / "provenance"
    index = (provenance_root / "index.json").read_bytes()
    journals = {
        exact_summary.journal_id: (
            provenance_root / "journals" / f"{exact_summary.journal_id}.json-seq"
        ).read_bytes()
    }
    validated = validate_portable_provenance_set(index, journals)
    assert summary.provenance_mode == "mixed"
    assert summary.provenance_journals == 1
    assert journals == {exact_summary.journal_id: exact_journal}
    assert index == build_portable_provenance_set(
        bindings=validated.bindings,
        journals=journals,
    )
    prepared = prepare_file_provenance(
        output / "notes" / "alpha.txt",
        relative_path="notes/alpha.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000002",
        agent_name="riverhog-client",
        agent_version="1.0.0",
        provenance=provenance_root,
    )
    assert prepared.journals == {exact_summary.journal_id: exact_journal}
    assert {path: (output / path).read_bytes() for path in expected} == expected
