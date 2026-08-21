"""Strict target-owned preservation of source-container artifacts."""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue
from stove0_media_archive_contracts import Av1OpusArchiveIntent

from stove0_nvenc_av1_opus_target import source_artifacts


def _run_checked(command: Sequence[str], label: str) -> None:
    result = subprocess.run(list(command), capture_output=True, check=False)
    if result.returncode == 0:
        return
    detail = (
        result.stderr.decode("utf-8", "replace").strip()
        or result.stdout.decode("utf-8", "replace").strip()
        or f"exit code {result.returncode}"
    )
    raise RuntimeError(f"{label} failed: {detail}")


def _export_auxiliary_streams(
    source: Path,
    exports: Sequence[Mapping[str, Any]],
) -> None:
    for export in exports:
        spec = export.get("spec")
        if not isinstance(spec, str) or not spec:
            continue
        export_path = Path(str(export["path"]))
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.unlink(missing_ok=True)
        stream_type = str(export.get("stype") or "")
        muxer = str(export.get("muxer") or "matroska")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            *source_artifacts.FFMPEG_INPUT_FLAGS,
            "-i",
            str(source),
            "-map",
            f"0:{spec}",
            *source_artifacts._metadata_copy_args([stream_type]),
            *source_artifacts.FFMPEG_OUTPUT_FLAGS,
            "-c",
            "copy",
            "-f",
            muxer,
            str(export_path),
        ]
        if muxer == "data":
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                *source_artifacts.FFMPEG_INPUT_FLAGS,
                "-i",
                str(source),
                "-map",
                f"0:{spec}",
                "-c",
                "copy",
                "-f",
                "data",
                str(export_path),
            ]
        _run_checked(command, f"source-artifact export for stream {spec}")
        source_artifacts._repair_iso_bmff_stream_artifact_sample_entry(
            source,
            export_path,
            cast(dict[str, Any], export.get("stream", {})),
        )
        if not export_path.is_file():
            raise RuntimeError(f"source-artifact export is missing: {export_path.name}")


def _maybe_copy_matroska_data_tracks(
    source: Path,
    archive: Path,
    data_streams: Sequence[Mapping[str, Any]],
) -> dict[str, JsonValue] | None:
    if not data_streams:
        return None
    mkvmerge = shutil.which("mkvmerge")
    if mkvmerge is None:
        raise RuntimeError("mkvmerge is required to preserve Matroska/WebM data tracks")
    part = archive.with_name(f".{archive.name}.source-data.part")
    part.unlink(missing_ok=True)
    command = [
        mkvmerge,
        "-o",
        str(part),
        "--disable-track-statistics-tags",
        str(archive),
        "--no-video",
        "--no-audio",
        "--no-subtitles",
        "--no-buttons",
        "--no-attachments",
        "--no-track-tags",
        "--no-global-tags",
        "--no-chapters",
        str(source),
    ]
    try:
        _run_checked(command, "Matroska/WebM data-track preservation remux")
        os.replace(part, archive)
    finally:
        part.unlink(missing_ok=True)
    return {"tool": "mkvmerge", "data_tracks_copied": len(data_streams)}


def _output_indices(
    stream_infos: Sequence[source_artifacts.StreamInfo],
    stream_type: str,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for info in sorted(stream_infos, key=lambda item: item["index"]):
        spec = info.get("spec")
        if info["stype"] == stream_type and isinstance(spec, str) and spec:
            result[spec] = len(result)
    return result


def _canonical_metadata(metadata: dict[str, Any], source: Path) -> dict[str, Any]:
    result = copy.deepcopy(metadata)
    raw_format = result.get("format")
    if isinstance(raw_format, dict) and "filename" in raw_format:
        raw_format["filename"] = source.name
    return result


def _canonical_command(command: Sequence[str], source: Path, archive: Path) -> list[str]:
    replacements = {str(source): "{source}", str(archive): "{archive}"}
    return [replacements.get(str(item), str(item)) for item in command]


def build_strict_source_artifacts(
    *,
    source: Path,
    archive: Path,
    bundle: Path,
    encode_command: Sequence[str],
    intent: Av1OpusArchiveIntent,
    target_options: Mapping[str, JsonValue],
    target_contract_sha256: str,
    plan_sha256: str,
) -> dict[str, JsonValue]:
    """Preserve and verify every non-archive source-container artifact.

    The operation contract has no omission policy, so every stream and relevant
    container artifact must be represented by the archive or this bundle.
    """

    work = bundle.parent / f".{bundle.name}.work"
    part = bundle.with_name(f".{bundle.name}.part.zst")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    part.unlink(missing_ok=True)
    try:
        dumped = source_artifacts._dump_streams_and_metadata(
            str(source),
            work,
            drop_policy=source_artifacts.SourceArtifactDropPolicy(()),
            allow_conversion_only_container=False,
        )
        exports = dumped["exports"]
        stream_infos = dumped["stream_infos"]
        dropped = dumped["dropped_items"]
        if dropped:
            raise RuntimeError("the v1 media operation does not permit source-artifact omission")

        container_mode = str(dumped["source_container"].get("mode") or "")
        data_infos = [
            info
            for info in stream_infos
            if info["stype"] == "d" and container_mode in {"matroska_rebuild", "webm_rebuild"}
        ]
        data_specs = {
            spec for info in data_infos if isinstance((spec := info.get("spec")), str) and spec
        }
        accounting_errors = source_artifacts._unaccounted_stream_errors(
            stream_infos,
            exports,
            dropped,
            data_copy_specs=data_specs,
        )
        if accounting_errors:
            raise RuntimeError("; ".join(accounting_errors))

        _export_auxiliary_streams(source, exports)
        matroska_remux = _maybe_copy_matroska_data_tracks(source, archive, data_infos)
        data_output_indices = {
            str(info["spec"]): index
            for index, info in enumerate(data_infos)
            if isinstance(info.get("spec"), str) and info.get("spec")
        }
        arcnames = source_artifacts._assign_source_stream_artifact_arcnames(
            dumped["container_artifacts"],
            exports,
        )
        stream_transforms = source_artifacts._stream_transform_records(
            stream_infos,
            exports,
            dropped,
            video_output_indices=_output_indices(stream_infos, "v"),
            audio_output_indices=_output_indices(stream_infos, "a"),
            subtitle_output_indices=_output_indices(stream_infos, "s"),
            attachment_output_indices=_output_indices(stream_infos, "t"),
            data_output_indices=data_output_indices,
            src_video_copy=set(),
            src_audio_copy=set(),
            selected_output_path=archive,
            encode_output_path=archive,
            use_constant_quality=True,
            constant_quality=intent.quality,
            global_video_kbps=0,
            audio_kbps=intent.audio_bitrate_kbps,
            svt_lp=0,
            video_codec="av1_nvenc",
            video_encoder={
                "target_contract_sha256": target_contract_sha256,
                "target_plan_sha256": plan_sha256,
                "target_options": dict(target_options),
                "portable_intent": intent.model_dump(mode="json"),
            },
            audio_codec="libopus",
            audio_encoder={"bitrate_kbps": intent.audio_bitrate_kbps},
            stream_artifact_arcnames=arcnames,
        )
        artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
            work_dir=work,
            src=str(source),
            output=archive.name,
            source_metadata=_canonical_metadata(dumped["source_metadata"], source),
            source_container=dumped["source_container"],
            container_inventory=dumped["container_inventory"],
            container_artifacts=dumped["container_artifacts"],
            exports=exports,
            stream_transforms=stream_transforms,
            dropped_items=dropped,
            encode_cmd=_canonical_command(encode_command, source, archive),
            selected_output_path=archive,
            encode_output_path=archive,
        )
        if not source_artifacts._build_source_artifacts_bundle(
            part,
            artifacts,
            src=str(source),
            output=archive.name,
            source_container=dumped["source_container"],
        ):
            raise RuntimeError("source-artifact bundle contains no recovery evidence")
        audit = source_artifacts._audit_source_artifacts_bundle(part, archive_mkv=archive)
        errors = cast(list[str], audit.get("errors") or [])
        if errors:
            raise RuntimeError("source-artifact audit failed: " + "; ".join(errors))
        if not audit.get("rebuild_supported"):
            warnings = cast(list[str], audit.get("warnings") or [])
            raise RuntimeError(
                "source artifacts do not support source-container rebuild"
                + (": " + "; ".join(warnings) if warnings else "")
            )
        bundle.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, bundle)
        return {
            "bytes": bundle.stat().st_size,
            "sha256": source_artifacts._sha256_path(bundle),
            "audit": cast(dict[str, JsonValue], audit),
            "matroska_remux": matroska_remux,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
        part.unlink(missing_ok=True)


__all__ = ["build_strict_source_artifacts"]
