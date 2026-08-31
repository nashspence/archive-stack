from __future__ import annotations

import hashlib
import json
import tarfile
from io import BytesIO

from riverhog_age import CHUNK_SIZE
from riverhog_core.domain.archive import ArchiveFile, PackMemberPlan, PackVolumePlan
from riverhog_core.pack_volume import (
    PACK_INDEX_PATH,
    PACK_PADDING_PREFIX,
    _alignment_padding,
    _pack_index_bytes,
    _tar_header,
    _tar_padding,
    iter_render_pack_upload_unit,
    iter_render_pack_upload_unit_payload,
    pack_unit_descriptors,
    plan_pack_volume,
)
from riverhog_protocol.pack_ingress import canonical_json_bytes

TEST_ARCHIVE_PART_BYTES = 5 * 1024 * 1024


def _file(path: str, content: bytes) -> ArchiveFile:
    return ArchiveFile(path=path, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


def _pack_plaintext(plan: PackVolumePlan, contents: dict[str, bytes]) -> bytes:
    return b"".join(
        chunk
        for unit in plan.units
        for chunk in iter_render_pack_upload_unit(
            plan,
            unit.unit,
            lambda path: (contents[path],),
        )
    )


class _SequentialPackAssembler:
    """Test-only append-as-members-finalize construction of a sealed pack."""

    def __init__(self, *, sequence: int, part_plaintext_bytes: int) -> None:
        self.sequence = sequence
        self.part_plaintext_bytes = part_plaintext_bytes
        self.volume_id = f"pack-{sequence:064x}"
        self.members: list[PackMemberPlan] = []
        self.plaintext = bytearray()
        self.unit = 0
        self.unit_start = 0

    def append(self, current: ArchiveFile, content: bytes) -> None:
        if self.members and len(self.plaintext) - self.unit_start >= self.part_plaintext_bytes:
            padding, end_offset = _alignment_padding(
                volume_id=self.volume_id,
                unit=self.unit,
                cursor=len(self.plaintext),
            )
            if padding is not None:
                assert padding.header_offset == len(self.plaintext)
                self.plaintext.extend(_tar_header(padding.path, padding.payload_bytes))
                self.plaintext.extend(b"\0" * padding.payload_bytes)
                self.plaintext.extend(b"\0" * _tar_padding(padding.payload_bytes))
            assert len(self.plaintext) == end_offset
            self.unit += 1
            self.unit_start = end_offset

        assert len(content) == current.bytes
        assert hashlib.sha256(content).hexdigest() == current.sha256
        header_offset = len(self.plaintext)
        header = _tar_header(current.path, current.bytes)
        data_offset = header_offset + len(header)
        self.plaintext.extend(header)
        self.plaintext.extend(content)
        self.plaintext.extend(b"\0" * _tar_padding(current.bytes))
        self.members.append(
            PackMemberPlan(
                path=current.path,
                bytes=current.bytes,
                sha256=current.sha256,
                unit=self.unit,
                header_offset=header_offset,
                data_offset=data_offset,
                end_offset=len(self.plaintext),
            )
        )

    def seal(self) -> tuple[tuple[PackMemberPlan, ...], bytes, bytes]:
        members = tuple(self.members)
        index_bytes = _pack_index_bytes(
            volume_id=self.volume_id,
            sequence=self.sequence,
            members=members,
        )
        self.plaintext.extend(_tar_header(PACK_INDEX_PATH, len(index_bytes)))
        self.plaintext.extend(index_bytes)
        self.plaintext.extend(b"\0" * _tar_padding(len(index_bytes)))
        self.plaintext.extend(b"\0" * 1024)
        return members, index_bytes, bytes(self.plaintext)


def test_sequential_pack_seal_matches_canonical_v1_pack() -> None:
    long_path = "nested/" + ("long-name-" * 14) + "file.txt"
    cases = (
        {
            long_path: b"long path content",
            "zero": b"",
        },
        {f"files/{index:02d}.bin": bytes([index]) * (1024 * 1024) for index in range(7)},
    )

    for sequence, contents in enumerate(cases):
        files = [_file(path, contents[path]) for path in sorted(contents)]
        plan = plan_pack_volume(
            files,
            sequence=sequence,
            part_plaintext_bytes=TEST_ARCHIVE_PART_BYTES,
        )
        assembler = _SequentialPackAssembler(
            sequence=sequence,
            part_plaintext_bytes=TEST_ARCHIVE_PART_BYTES,
        )
        for current in files:
            assembler.append(current, contents[current.path])
        sequential_members, sequential_index, sequential_plaintext = assembler.seal()
        planned_plaintext = _pack_plaintext(plan, contents)

        assert sequential_members == plan.members
        assert sequential_index == plan.index_bytes
        assert hashlib.sha256(sequential_index).hexdigest() == plan.index_sha256
        assert sequential_plaintext == planned_plaintext
        assert len(sequential_plaintext) == plan.plaintext_bytes

        if sequence == 0:
            assert len(plan.units) == 1
            assert next(member for member in plan.members if member.path == "zero").bytes == 0
            assert (
                next(member for member in plan.members if member.path == long_path).data_offset
                > 512
            )
        else:
            assert len(plan.units) >= 2
            assert plan.units[0].padding is not None
            assert plan.units[-1].includes_index


def test_pack_plan_uses_age_aligned_nonfinal_units_and_is_deterministic() -> None:
    contents = {f"files/{index:02d}.bin": bytes([index]) * (1024 * 1024) for index in range(7)}
    files = [_file(path, content) for path, content in contents.items()]

    first = plan_pack_volume(
        files,
        sequence=0,
        part_plaintext_bytes=TEST_ARCHIVE_PART_BYTES,
    )
    second = plan_pack_volume(
        list(reversed(files)),
        sequence=0,
        part_plaintext_bytes=TEST_ARCHIVE_PART_BYTES,
    )

    assert first == second
    assert len(first.units) >= 2
    assert all(
        current.plaintext_bytes >= TEST_ARCHIVE_PART_BYTES
        and current.plaintext_end % CHUNK_SIZE == 0
        for current in first.units[:-1]
    )
    assert first.units[-1].final
    assert first.units[-1].includes_index
    assert first.plan_sha256 == pack_unit_descriptors(first)[0].plan_sha256


def test_rendered_pack_is_standard_tar_with_embedded_verified_index() -> None:
    long_path = "nested/" + ("long-name-" * 14) + "file.txt"
    contents = {
        "alpha.txt": b"alpha",
        long_path: b"long path content",
        "zero": b"",
    }
    plan = plan_pack_volume(
        [_file(path, content) for path, content in contents.items()],
        sequence=3,
        part_plaintext_bytes=TEST_ARCHIVE_PART_BYTES,
    )

    plaintext = _pack_plaintext(plan, contents)

    assert len(plaintext) == plan.plaintext_bytes
    with tarfile.open(fileobj=BytesIO(plaintext), mode="r:") as archive:
        names = archive.getnames()
        assert set(contents).issubset(names)
        assert PACK_INDEX_PATH in names
        assert all(
            archive.extractfile(path).read() == content  # type: ignore[union-attr]
            for path, content in contents.items()
        )
        index_bytes = archive.extractfile(PACK_INDEX_PATH).read()  # type: ignore[union-attr]
    assert hashlib.sha256(index_bytes).hexdigest() == plan.index_sha256
    parsed = json.loads(index_bytes)
    assert set(parsed) == {"schema", "volume", "tree", "files"}
    assert parsed["schema"] == "riverhog-pack-index/v1"
    assert parsed["volume"] == {"id": plan.volume_id, "sequence": 3}
    assert [row["path"] for row in parsed["files"]] == sorted(contents)
    assert all(
        name in contents or name == PACK_INDEX_PATH or name.startswith(PACK_PADDING_PREFIX)
        for name in names
    )


def test_pack_unit_wire_payload_is_only_concatenated_source_bytes() -> None:
    contents = {"a": b"abc", "b": b"defgh"}
    plan = plan_pack_volume(
        [_file(path, content) for path, content in contents.items()],
        sequence=0,
    )
    descriptor = pack_unit_descriptors(plan)[0]
    payload = b"".join(contents[source.path] for source in descriptor.sources)

    rendered = b"".join(iter_render_pack_upload_unit_payload(plan, 0, (payload[:4], payload[4:])))

    assert len(rendered) == plan.units[0].plaintext_bytes
    with tarfile.open(fileobj=BytesIO(rendered), mode="r:") as archive:
        assert archive.extractfile("a").read() == b"abc"  # type: ignore[union-attr]
        assert archive.extractfile("b").read() == b"defgh"  # type: ignore[union-attr]


def test_pack_index_and_upload_plan_are_canonical_json() -> None:
    content = b"schema"
    plan = plan_pack_volume([_file("schema.txt", content)], sequence=0)
    index = json.loads(plan.index_bytes)
    upload_plan = {
        "schema": "pack-upload-plan/v1",
        "volume_id": plan.volume_id,
        "plaintext_bytes": plan.plaintext_bytes,
        "units": [
            {
                "unit": descriptor.unit,
                "payload_bytes": descriptor.payload_bytes,
                "plaintext_start": descriptor.plaintext_start,
                "plaintext_bytes": descriptor.plaintext_bytes,
                "final": descriptor.final,
                "sources": [
                    {"path": source.path, "bytes": source.bytes, "sha256": source.sha256}
                    for source in descriptor.sources
                ],
            }
            for descriptor in pack_unit_descriptors(plan)
        ],
    }

    assert index["schema"] == "riverhog-pack-index/v1"
    assert canonical_json_bytes(upload_plan)


def test_server_pack_plan_recipe_round_trips() -> None:
    from riverhog_core.pack_volume import (
        pack_volume_plan_payload,
        parse_pack_volume_plan,
    )

    contents = {"a.txt": b"alpha", "nested/b.txt": b"beta"}
    plan = plan_pack_volume(
        [_file(path, content) for path, content in contents.items()],
        sequence=7,
        part_plaintext_bytes=TEST_ARCHIVE_PART_BYTES,
    )
    payload = pack_volume_plan_payload(plan)
    rebuilt = parse_pack_volume_plan(canonical_json_bytes(payload))
    assert payload["schema"] == "pack-volume-plan/v1"
    assert rebuilt == plan
