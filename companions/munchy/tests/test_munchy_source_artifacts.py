from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from munchy_target_support import source_artifacts
from munchy_target_support.source_artifact_bridge import (
    _allow_conversion_only_container,
    _artifact_drop_reason_map,
    build_preserve_source_artifacts,
)


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
    assert all(not str(item["path"]).startswith("rebuild/") for item in manifest["artifacts"])

    audit = source_artifacts._audit_source_artifacts_bundle(bundle_path)

    assert audit["ok"] is True
    assert audit["rebuild_supported"] is True
    assert audit["artifacts_checked"] == len(artifacts)


def test_source_artifact_bundle_preserves_media_inventory(tmp_path: Path) -> None:
    artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
        work_dir=tmp_path / "work",
        src="clip.mp4",
        output="clip.mkv",
        source_metadata={"format": {"format_name": "mov"}, "streams": []},
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

    assert artifacts
    assert any(artifact.kind == "source_ffprobe" for artifact in artifacts)


def test_preserve_source_artifacts_reports_no_additional_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "clip.bin"
    source.write_bytes(b"source")

    result = build_preserve_source_artifacts(
        source=source,
        output=source,
    )

    assert result == {
        "omitted": True,
        "reason": "no independently retained source artifacts were produced",
    }


def test_source_artifact_bundle_has_no_rebuild_directory(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "work"
    bundle_path = tmp_path / "voice.opus.source-artifacts.tar"
    artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
        work_dir=work_dir,
        src="voice.mp3",
        output="voice.opus",
        source_metadata={"format": {}, "streams": []},
        source_container={
            "supported": False,
            "mode": "conversion_only",
            "message": "source container is not currently rebuild-supported",
            "override": "allow_conversion_only_container",
        },
        container_inventory=[],
        container_artifacts=[],
        exports=[],
        stream_transforms=[],
        dropped_items=[],
        encode_cmd=["ffmpeg", "-i", "voice.mp3", "voice.opus"],
        selected_output_path=tmp_path / "voice.opus",
        encode_output_path=tmp_path / "voice.opus",
    )

    assert all(not artifact.arcname.startswith("rebuild/") for artifact in artifacts)
    assert not (work_dir / "rebuild").exists()

    created = source_artifacts._build_source_artifacts_bundle(
        bundle_path,
        artifacts,
        src="voice.mp3",
        output="voice.opus",
    )

    assert created is True
    with tarfile.open(bundle_path, "r") as tar:
        names = set(tar.getnames())
    assert all(not name.startswith("rebuild/") for name in names)

    audit = source_artifacts._audit_source_artifacts_bundle(bundle_path)

    assert audit["ok"] is True
    assert audit["rebuild_supported"] is False
    assert audit["rebuild_blockers"] == ["source_container"]
    assert audit["rebuild_scope"] == (
        "conversion-only archive; source-container rebuild is not supported"
    )


def test_preserve_source_artifacts_include_evidence_sidecars(tmp_path: Path) -> None:
    if shutil.which("zstd") is None:
        pytest.skip("zstd is not installed")
    source = tmp_path / "IMG_0001.HEIC"
    output = tmp_path / "archive" / "IMG_0001.HEIC"
    sidecar = tmp_path / "IMG_0001.HEIC.xmp"
    source.write_bytes(b"heic")
    output.parent.mkdir()
    output.write_bytes(b"heic")
    sidecar.write_text("<xmp/>", encoding="utf-8")

    result = build_preserve_source_artifacts(
        source=source,
        output=output,
        source_sidecars=[
            {
                "id": "xmp",
                "format": "xmp",
                "path": sidecar,
                "arcname": "sidecars/IMG_0001.HEIC.xmp",
                "source_rel_path": "IMG_0001.HEIC.xmp",
            }
        ],
    )

    bundle_path = Path(result["output"])
    audit = source_artifacts._audit_source_artifacts_bundle(bundle_path)

    assert audit["ok"] is True
    assert audit["rebuild_supported"] is True
    assert audit["rebuild_scope"] == "primary source bytes are preserved as the archive output"

    extract_dir = tmp_path / "extracted"
    source_artifacts._safe_extract_source_artifacts(bundle_path, extract_dir)
    manifest = json.loads((extract_dir / "manifest.json").read_text(encoding="utf-8"))
    names = {
        path.relative_to(extract_dir).as_posix()
        for path in extract_dir.rglob("*")
        if path.is_file()
    }
    assert "sidecars/IMG_0001.HEIC.xmp" in names
    assert "inventory/source-ffprobe.json" not in names
    assert "inventory/source-inventory.json" not in names
    assert "encoding/stream-transforms.json" not in names
    assert manifest["source_container"]["mode"] == "preserve_primary_bytes"
    assert (extract_dir / "sidecars" / "IMG_0001.HEIC.xmp").read_text(encoding="utf-8") == "<xmp/>"
    assert any(
        item["kind"] == "source_sidecar" and item["path"] == "sidecars/IMG_0001.HEIC.xmp"
        for item in manifest["artifacts"]
    )


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
        source_artifacts._source_artifacts_path("clip.webm") == "clip.webm.source-artifacts.tar.zst"
    )


def test_source_artifact_bridge_accepts_service_encode_profile_shape() -> None:
    profile = {
        "schema_version": 1,
        "target": "munchy-av1-nvenc",
        "name": "camera-preview",
        "archive": {
            "codec": "av1_nvenc",
            "container": "webm",
            "quality": 49,
            "max_height": 720,
            "fps_mode": "passthrough",
            "scale_flags": "lanczos",
            "pix_fmt": "p010le",
            "preset": "p7",
            "tune": "uhq",
            "audio": {
                "codec": "opus",
                "bitrate": "28k",
                "sample_rate": 24000,
                "channels": 1,
                "application": "audio",
                "frame_duration": 40.0,
                "cutoff": 12000,
                "compression_level": 10,
                "vbr": "on",
            },
        },
        "source": {
            "allow_conversion_only_container": True,
            "artifact_drops": [
                {"selector": " Stream:7 ", "reason": "not useful after stabilization"}
            ],
        },
    }

    assert _allow_conversion_only_container(profile) is True
    assert _artifact_drop_reason_map(profile) == {"stream:7": "not useful after stabilization"}


def test_source_artifact_bridge_rejects_non_boolean_conversion_only_override() -> None:
    with pytest.raises(ValueError, match="allow_conversion_only_container must be a boolean"):
        _allow_conversion_only_container({"source": {"allow_conversion_only_container": "yes"}})
