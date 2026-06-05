from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from munchy import source_artifacts


def test_source_artifact_bundle_uses_munchy_manifest_kind(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    bundle_path = tmp_path / "clip.mkv.source-artifacts.tar"
    artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
        work_dir=work_dir,
        src="clip.mp4",
        output="clip.mkv",
        source_metadata={"format": {}, "streams": []},
        source_container={"supported": True, "mode": "iso_bmff_rebuild"},
        container_inventory=[],
        container_artifacts=[],
        exports=[],
        stream_transforms=[],
        dropped_items=[],
        encode_cmd=["ffmpeg", "-i", "clip.mp4", "clip.mkv"],
        selected_output_path=tmp_path / "clip.mkv",
        encode_output_path=tmp_path / "clip.mkv",
    )

    created = source_artifacts._build_source_artifacts_bundle(
        bundle_path,
        artifacts,
        src="clip.mp4",
        output="clip.mkv",
    )

    assert created is True
    with tarfile.open(bundle_path, "r") as tar:
        manifest_member = tar.extractfile("manifest.json")
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read().decode("utf-8"))
    assert manifest["kind"] == "munchy.source-artifacts"

    audit = source_artifacts._audit_source_artifacts_bundle(bundle_path)

    assert audit["ok"] is True
    assert audit["rebuild_supported"] is True
    assert audit["artifacts_checked"] == len(artifacts)


def test_source_artifact_audit_rejects_non_munchy_manifest_kind(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.bin"
    payload_path.write_bytes(b"payload")
    manifest = {
        "schema_version": 1,
        "kind": "other.source-artifacts",
        "source": "clip.mp4",
        "output": "clip.mkv",
        "artifacts": [
            {
                "path": "payload.bin",
                "kind": "payload",
                "description": "payload",
                "mime_type": "application/octet-stream",
                "bytes": payload_path.stat().st_size,
                "sha256": source_artifacts._sha256_path(payload_path),
            }
        ],
        "dropped": [],
    }
    bundle_path = tmp_path / "bad.source-artifacts.tar"
    with tarfile.open(bundle_path, "w") as tar:
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))
        tar.add(payload_path, arcname="payload.bin", recursive=False)

    audit = source_artifacts._audit_source_artifacts_bundle(bundle_path)

    assert audit["ok"] is False
    assert "manifest kind is not munchy.source-artifacts" in audit["errors"]


def test_source_artifact_default_path_is_zstd_tar() -> None:
    assert (
        source_artifacts._source_artifacts_path("clip.webm")
        == "clip.webm.source-artifacts.tar.zst"
    )
