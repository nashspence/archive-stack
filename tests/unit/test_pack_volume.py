from __future__ import annotations

import hashlib
import json
import tarfile
from io import BytesIO

from riverhog_age import CHUNK_SIZE, S3_MIN_PART_SIZE
from riverhog_core.domain.archive import ArchiveFile
from riverhog_core.pack_volume import (
    PACK_INDEX_PATH,
    PACK_PADDING_PREFIX,
    iter_pack_plaintext,
    pack_unit_descriptors,
    parse_pack_index,
    plan_pack_volume,
    render_pack_upload_unit_payload,
)
from riverhog_protocol.pack_ingress import canonical_json_bytes


def _file(path: str, content: bytes) -> ArchiveFile:
    return ArchiveFile(path=path, bytes=len(content), sha256=hashlib.sha256(content).hexdigest())


def test_pack_plan_uses_age_aligned_nonfinal_units_and_is_deterministic() -> None:
    contents = {f"files/{index:02d}.bin": bytes([index]) * (1024 * 1024) for index in range(7)}
    files = [_file(path, content) for path, content in contents.items()]

    first = plan_pack_volume(
        files,
        sequence=0,
        part_plaintext_bytes=S3_MIN_PART_SIZE,
    )
    second = plan_pack_volume(
        list(reversed(files)),
        sequence=0,
        part_plaintext_bytes=S3_MIN_PART_SIZE,
    )

    assert first == second
    assert len(first.units) >= 2
    assert all(
        current.plaintext_bytes >= S3_MIN_PART_SIZE and current.plaintext_end % CHUNK_SIZE == 0
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
        part_plaintext_bytes=S3_MIN_PART_SIZE,
    )

    plaintext = b"".join(iter_pack_plaintext(plan, lambda path: (contents[path],)))

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
    parsed = parse_pack_index(index_bytes)
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

    rendered = render_pack_upload_unit_payload(plan, 0, (payload[:4], payload[4:]))

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
        part_plaintext_bytes=S3_MIN_PART_SIZE,
    )
    payload = pack_volume_plan_payload(plan)
    rebuilt = parse_pack_volume_plan(canonical_json_bytes(payload))
    assert payload["schema"] == "pack-volume-plan/v1"
    assert rebuilt == plan
