from __future__ import annotations

import yaml

from arc_core.planner.manifest import (
    assign_collection_artifact_paths,
    manifest_collection_budget,
    manifest_file_entry,
    recovery_readme_bytes,
    sidecar_bytes,
    sidecar_dict,
)


def test_assign_collection_artifact_paths_sorts_ids_and_zero_pads_names() -> None:
    paths = assign_collection_artifact_paths({"zeta", "alpha"})

    assert list(paths) == ["alpha", "zeta"]
    assert paths["alpha"] == ("collections/000001.yml.age", "collections/000001.ots.age")
    assert paths["zeta"] == ("collections/000002.yml.age", "collections/000002.ots.age")


def test_manifest_file_entry_includes_optional_object_sidecar_and_parts_fields() -> None:
    entry = manifest_file_entry(
        " tax/2022/report.pdf ",
        "a" * 64,
        plaintext_bytes=123,
        object_path="files/000001.age",
        sidecar_path="files/000001.yml.age",
        parts={"count": 2, "present": [{"index": 1}]},
    )

    assert entry == {
        "path": "/tax/2022/report.pdf",
        "sha256": "a" * 64,
        "bytes": 123,
        "object": "files/000001.age",
        "sidecar": "files/000001.yml.age",
        "parts": {"count": 2, "present": [{"index": 1}]},
    }


def test_sidecar_helpers_include_uid_gid_and_part_when_present() -> None:
    payload = {
        "relpath": " tax/2022/report.pdf ",
        "sha256": "b" * 64,
        "plaintext_bytes": 321,
        "mode": 0o644,
        "mtime": 1_713_614_400,
        "uid": 1000,
        "gid": 1001,
    }

    sidecar = sidecar_dict(payload, collection_id="docs", part_index=1, part_count=3)
    encoded = yaml.safe_load(
        sidecar_bytes(payload, collection_id="docs", part_index=1, part_count=3).decode("utf-8")
    )

    assert sidecar == encoded
    assert sidecar["path"] == "/tax/2022/report.pdf"
    assert sidecar["uid"] == 1000
    assert sidecar["gid"] == 1001
    assert sidecar["part"] == {"index": 2, "count": 3}


def test_sidecar_dict_omits_uid_gid_and_part_for_unsplit_files() -> None:
    sidecar = sidecar_dict(
        {
            "relpath": "/tax/2022/report.pdf",
            "sha256": "c" * 64,
            "plaintext_bytes": 111,
            "mode": 0o600,
            "mtime": 1_700_000_000,
        },
        collection_id="docs",
    )

    assert "uid" not in sidecar
    assert "gid" not in sidecar
    assert "part" not in sidecar


def test_manifest_collection_budget_grows_with_more_files_and_readme_mentions_recovery_steps(
) -> None:
    one_file = manifest_collection_budget(
        "docs",
        [{"relpath": "/a.txt", "sha256": "d" * 64, "plaintext_bytes": 10}],
    )
    two_files = manifest_collection_budget(
        "docs",
        [
            {"relpath": "/a.txt", "sha256": "d" * 64, "plaintext_bytes": 10},
            {"relpath": "/b.txt", "sha256": "e" * 64, "plaintext_bytes": 20},
        ],
    )
    readme = recovery_readme_bytes("img_001").decode("utf-8")

    assert one_file > 0
    assert two_files > one_file
    assert "Archive image: img_001" in readme
    assert "decrypt DISC.yml.age" in readme
    assert "collections/*.ots.age" in readme
