from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import riverhog_recover.recovery as recovery_module
from riverhog_age import encrypt_age_scrypt
from riverhog_archive_contracts import (
    ARCHIVE_ENCRYPTION_FORMAT,
    ArchiveRootCiphertextIdentity,
    CollectionEncryptionBinding,
    RecoveryDescriptor,
    format_archive_sequence,
)
from riverhog_core.archive_manifest import (
    build_collection_archive_authority,
    build_collection_archive_terminal_document,
)
from riverhog_core.domain.archive import (
    ArchiveFile,
    SealedPackVolume,
    SealedProvenanceObject,
    SealedRawVolume,
    StoredArchivePart,
    VerifiedRawFile,
)
from riverhog_core.pack_volume import iter_render_pack_upload_unit, plan_pack_volume
from riverhog_core.raw_verification import raw_file_ordered_volume_commitment
from riverhog_protocol import (
    COLLECTION_DESCRIPTION_RELATIVE_PATH,
    COLLECTION_TAG_HEAD_RELATIVE_PATH,
    CollectionDescriptionDocument,
    CollectionTagHeadDocument,
    CollectionTagSet,
    MemoryCollectionTagNodeStore,
    collection_tag_node_path,
)
from riverhog_provenance import (
    PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX,
    FileProvenanceBinding,
    ProvenancePayloadIdentity,
    ProvenanceRootDocument,
    ProvenanceTerminalDocument,
    ProvenanceVolumeDocument,
    binding_segment_bytes,
    create_derivative_journal_from_identity,
    create_observation_journal,
    format_provenance_sequence,
    prepare_file_provenance,
    update_ordered_volume_commitment,
    validate_journal,
)
from riverhog_recover import (
    RecoveryError,
    recover_archive,
    recover_collection_description,
    recover_collection_tags,
)

from tests.fixtures.archive import age_state_json
from tests.provenance_observer import native_provenance_observer

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


def _write_archive(
    root: Path,
    *,
    passphrase: str = PASSPHRASE,
    passphrase_id: str = PASSPHRASE_ID,
    with_provenance: bool = False,
    provenance_journal: bytes | None = None,
    provenance_journals: Mapping[str, bytes] | None = None,
    description: str | None = None,
    tags: Sequence[str] = (),
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
        volume_id = f"segment-{format_archive_sequence(sequence)}"
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
        ordered_volume_sha256=raw_file_ordered_volume_commitment(
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
                observer=native_provenance_observer(),
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
        tree_digest = hashlib.sha256()
        for current in files:
            tree_digest.update(f"{current.path}\t{current.bytes}\t{current.sha256}\n".encode())
        tree_sha256 = tree_digest.hexdigest()
        binding_payload = binding_segment_bytes(
            first_file_order=0,
            files=[
                {
                    "path": binding.path,
                    "bytes": binding.bytes,
                    "sha256": binding.sha256,
                    "status": binding.status,
                    **(
                        {
                            "journal_id": binding.journal_id,
                            "current_state_id": binding.current_state_id,
                        }
                        if binding.status == "captured"
                        else {"omission_reason": binding.omission_reason}
                    ),
                }
                for binding in bindings
            ],
        )
        volume_documents: list[ProvenanceVolumeDocument] = []
        volume_payloads: list[bytes] = []
        sequence = 0
        volume_documents.append(
            ProvenanceVolumeDocument(
                archive_generation="a" * 64,
                archive_tree_sha256=tree_sha256,
                sequence=sequence,
                payload=ProvenancePayloadIdentity(
                    kind="bindings",
                    path=f"provenance/payloads/volume-{format_provenance_sequence(sequence)}.bin.age",
                    bytes=len(binding_payload),
                    sha256=_sha256(binding_payload),
                ),
                first_file_order=0,
                file_count=len(bindings),
            )
        )
        volume_payloads.append(binding_payload)
        sequence += 1
        for journal_id, content in sorted(journal_set.items()):
            for offset in range(0, len(content), PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX):
                payload = content[offset : offset + PROVENANCE_JOURNAL_SEGMENT_BYTES_MAX]
                volume_documents.append(
                    ProvenanceVolumeDocument(
                        archive_generation="a" * 64,
                        archive_tree_sha256=tree_sha256,
                        sequence=sequence,
                        payload=ProvenancePayloadIdentity(
                            kind="journal",
                            path=(
                                "provenance/payloads/volume-"
                                f"{format_provenance_sequence(sequence)}.bin.age"
                            ),
                            bytes=len(payload),
                            sha256=_sha256(payload),
                        ),
                        journal_id=journal_id,
                        journal_offset=offset,
                        journal_bytes=len(content),
                        journal_sha256=_sha256(content),
                    )
                )
                volume_payloads.append(payload)
                sequence += 1
        ordered = hashlib.sha256()
        for document, payload in zip(volume_documents, volume_payloads, strict=True):
            metadata_bytes = document.to_json_bytes()
            update_ordered_volume_commitment(ordered, document)
            provenance_ciphertexts[document.metadata_path] = encrypt_age_scrypt(
                metadata_bytes, passphrase, log_n=1
            )
            provenance_ciphertexts[document.payload.path] = encrypt_age_scrypt(
                payload, passphrase, log_n=1
            )
        provenance_terminal = ProvenanceTerminalDocument(
            archive_generation="a" * 64,
            archive_tree_sha256=tree_sha256,
            sequence=len(volume_documents),
        )
        update_ordered_volume_commitment(ordered, provenance_terminal)
        provenance_ciphertexts[provenance_terminal.metadata_path] = encrypt_age_scrypt(
            provenance_terminal.to_json_bytes(), passphrase, log_n=1
        )
        provenance_root = ProvenanceRootDocument(
            archive_generation="a" * 64,
            archive_tree_sha256=tree_sha256,
            ordered_volume_sha256=ordered.hexdigest(),
        )
        root_plaintext = provenance_root.to_json_bytes()
        root_ciphertext = encrypt_age_scrypt(root_plaintext, passphrase, log_n=1)
        provenance_ciphertexts["provenance/root.json.age"] = root_ciphertext
        provenance_identity = provenance_root.identity
        provenance_objects = (
            SealedProvenanceObject(
                object_id="provenance-root",
                kind="provenance-root",
                relative_path="provenance/root.json.age",
                plaintext_bytes=len(root_plaintext),
                plaintext_sha256=provenance_root.identity,
                stored_bytes=len(root_ciphertext),
                stored_sha256=_sha256(root_ciphertext),
                revision="provenance-root-version",
                completed_at="2026-08-08T00:00:00Z",
            ),
        )

    manifest, volume_documents = build_collection_archive_authority(
        archive_generation="a" * 64,
        files=files,
        packs=((pack_plan, sealed_pack),),
        raw_volumes=raw_volumes,
        verified_raw_files=(verified_raw,),
        provenance_identity=provenance_identity,
        provenance_objects=provenance_objects,
    )
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
        "recovery.json": descriptor,
        sealed_pack.relative_path: pack_ciphertext,
        **raw_ciphertexts,
        **provenance_ciphertexts,
    }
    if description is not None:
        description_document = CollectionDescriptionDocument.seal(
            archive_root_sha256=_sha256(manifest),
            revision=1,
            description=description,
        )
        ciphertext[COLLECTION_DESCRIPTION_RELATIVE_PATH] = encrypt_age_scrypt(
            description_document.to_json_bytes(),
            passphrase,
            log_n=1,
        )
    tag_store = MemoryCollectionTagNodeStore()
    tag_set = CollectionTagSet(tag_store)
    for tag in tags:
        tag_set = tag_set.insert(tag)
    tag_head = CollectionTagHeadDocument.seal(
        archive_root_sha256=_sha256(manifest),
        revision=1,
        root_sha256=tag_set.root.root_sha256,
    )
    ciphertext[COLLECTION_TAG_HEAD_RELATIVE_PATH] = encrypt_age_scrypt(
        tag_head.to_json_bytes(), passphrase, log_n=1
    )
    for digest, encoded in tag_store.nodes.items():
        ciphertext[collection_tag_node_path(digest)] = encrypt_age_scrypt(
            encoded, passphrase, log_n=1
        )
    for document in volume_documents:
        relative_path = (
            f"metadata/volume-{format_archive_sequence(document.volume.sequence)}.json.age"
        )
        ciphertext[relative_path] = encrypt_age_scrypt(
            document.to_json_bytes(),
            passphrase,
            log_n=1,
        )
    tree_sha256 = str(__import__("json").loads(manifest)["tree"]["sha256"])
    archive_terminal = build_collection_archive_terminal_document(
        archive_generation="a" * 64,
        tree_sha256=tree_sha256,
        sequence=len(volume_documents),
    )
    terminal_path = f"metadata/volume-{format_archive_sequence(archive_terminal.sequence)}.json.age"
    ciphertext[terminal_path] = encrypt_age_scrypt(
        archive_terminal.to_json_bytes(), passphrase, log_n=1
    )
    for relative, content in ciphertext.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return expected, exact_journal


def test_recovers_complete_collection_without_server_or_database(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    expected, _journal = _write_archive(archive)
    output = tmp_path / "recovered"

    summary = recover_archive(
        archive,
        output,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
    )

    assert summary.files == len(expected)
    assert summary.bytes == sum(len(content) for content in expected.values())
    assert summary.volumes == 3
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_recovers_description_without_reading_collection_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive, description="Résumé of 東京 footage")
    decrypted: list[str] = []
    original_decrypt = recovery_module._age_decrypt

    def decrypt(source: Path, *args: object, **kwargs: object) -> None:
        decrypted.append(source.relative_to(archive).as_posix())
        original_decrypt(source, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(recovery_module, "_age_decrypt", decrypt)
    document = recover_collection_description(
        archive,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
    )

    assert document is not None
    assert document.description == "Résumé of 東京 footage"
    assert len(document.archive_root_sha256) == 64
    assert decrypted == ["manifest.json.age", COLLECTION_DESCRIPTION_RELATIVE_PATH]


def test_missing_description_is_an_explicitly_absent_optional_sidecar(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)

    assert (
        recover_collection_description(
            archive,
            passphrases={PASSPHRASE_ID: PASSPHRASE},
        )
        is None
    )


def test_recovers_exact_tags_without_reading_collection_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    expected = ("camera:七", "source:ftp", "z" * 65_536)
    _write_archive(archive, tags=expected)
    decrypted: list[str] = []
    original_decrypt = recovery_module._age_decrypt

    def decrypt(source: Path, *args: object, **kwargs: object) -> None:
        decrypted.append(source.relative_to(archive).as_posix())
        original_decrypt(source, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(recovery_module, "_age_decrypt", decrypt)
    recovered = recover_collection_tags(
        archive,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
    )

    assert set(recovered.iter_tags()) == set(expected)
    assert recovered.head.revision == 1
    assert decrypted[0:2] == ["manifest.json.age", COLLECTION_TAG_HEAD_RELATIVE_PATH]
    assert all(path.startswith("tags/nodes/") for path in decrypted[2:])
    assert not any(path.startswith("volumes/") for path in decrypted)


def test_tag_recovery_rejects_a_head_for_another_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive, tags=("source:ftp",))
    wrong = CollectionTagHeadDocument.seal(
        archive_root_sha256="f" * 64,
        revision=1,
        root_sha256=None,
    )
    (archive / COLLECTION_TAG_HEAD_RELATIVE_PATH).write_bytes(
        encrypt_age_scrypt(wrong.to_json_bytes(), PASSPHRASE, log_n=1)
    )

    with pytest.raises(RecoveryError, match="another archive root"):
        recover_collection_tags(
            archive,
            passphrases={PASSPHRASE_ID: PASSPHRASE},
        )


def test_tag_recovery_rejects_a_missing_authenticated_node(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive, tags=("source:ftp",))
    node = next((archive / "tags/nodes").glob("*/*.age"))
    node.unlink()
    recovered = recover_collection_tags(
        archive,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
    )

    with pytest.raises(RecoveryError, match="archive file is missing"):
        tuple(recovered.iter_tags())


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
        )
        assert {path: (output / path).read_bytes() for path in expected} == expected

    with pytest.raises(RecoveryError, match=second_id):
        recover_archive(
            second_archive,
            tmp_path / "missing-key-output",
            passphrases={PASSPHRASE_ID: PASSPHRASE},
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

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "riverhog_recover.cli",
            str(archive),
            str(output),
            "--passphrases-file",
            str(passphrases_file),
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
    damaged = archive / f"volumes/segment-{format_archive_sequence(1)}.bin.age"
    damaged.write_bytes(damaged.read_bytes() + b"damage")
    output = tmp_path / "recovered"
    with pytest.raises(RecoveryError, match="stored volume byte count mismatch"):
        recover_archive(
            archive,
            output,
            passphrases={PASSPHRASE_ID: PASSPHRASE},
        )

    assert not output.exists()


def test_recovery_resumes_after_last_durable_volume_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive"
    expected, _journal = _write_archive(archive)
    damaged = archive / f"volumes/segment-{format_archive_sequence(1)}.bin.age"
    original = damaged.read_bytes()
    damaged.write_bytes(original + b"damage")
    output = tmp_path / "recovered"
    with pytest.raises(RecoveryError, match="stored volume byte count mismatch"):
        recover_archive(
            archive,
            output,
            passphrases={PASSPHRASE_ID: PASSPHRASE},
        )

    checkpoint = tmp_path / ".recovered.riverhog-recovery" / "state.sqlite3"
    assert checkpoint.is_file()
    damaged.write_bytes(original)
    original_recover_pack = recovery_module._recover_pack
    pack_calls = 0

    def recover_pack(*args: object, **kwargs: object) -> object:
        nonlocal pack_calls
        pack_calls += 1
        return original_recover_pack(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(recovery_module, "_recover_pack", recover_pack)
    recover_archive(
        archive,
        output,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
    )

    assert pack_calls == 0
    assert not checkpoint.parent.exists()
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_recovery_descriptor_rejects_changed_root_before_decryption(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    root = archive / "manifest.json.age"
    root.write_bytes(root.read_bytes() + b"changed")

    with pytest.raises(RecoveryError, match="does not match recovery descriptor"):
        recover_archive(
            archive,
            tmp_path / "output",
            passphrases={PASSPHRASE_ID: PASSPHRASE},
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
        observer=native_provenance_observer(),
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

    summary = recover_archive(
        archive,
        output,
        passphrases={PASSPHRASE_ID: PASSPHRASE},
    )

    provenance_root = output / ".riverhog" / "provenance"
    restored_root = ProvenanceRootDocument.from_json_bytes(
        (provenance_root / "root.json").read_bytes()
    )
    restored_journals = {
        journal_id: (provenance_root / "journals" / f"{journal_id}.json-seq").read_bytes()
        for journal_id in journals
    }
    assert summary.provenance_mode == "mixed"
    assert summary.provenance_journals == 2
    assert restored_journals == journals
    assert restored_root.ordered_volume_sha256
    assert len(list((provenance_root / "metadata").glob("*.json"))) == (
        len(list((provenance_root / "payloads").glob("*.bin"))) + 1
    )
    prepared = prepare_file_provenance(
        output / "notes" / "alpha.txt",
        relative_path="notes/alpha.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000002",
        agent_name="riverhog-client",
        agent_version="1.0.0",
        observer=native_provenance_observer(),
        provenance=provenance_root,
    )
    assert prepared.journals == journals
    assert {path: (output / path).read_bytes() for path in expected} == expected
