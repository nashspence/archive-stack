from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, cast

from munchy import source_artifacts
from munchy.profiles import normalize_artifact_drop_selector


def _run_checked(cmd: Sequence[str], label: str) -> None:
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        return
    detail = (
        proc.stderr.decode("utf-8", "replace").strip()
        or proc.stdout.decode("utf-8", "replace").strip()
        or f"exit code {proc.returncode}"
    )
    raise RuntimeError(f"{label} failed: {detail}")


def _bitrate_kbps(value: Any, default: int) -> int:
    if value is None:
        return default
    text = str(value).strip().lower()
    try:
        if text.endswith("k"):
            return max(1, int(float(text[:-1])))
        if text.endswith("m"):
            return max(1, int(float(text[:-1]) * 1000))
        return max(1, int(float(text) / 1000))
    except ValueError:
        return default


def _archive_profile(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    archive = profile.get("archive") if profile else None
    return archive if isinstance(archive, Mapping) else {}


def _audio_profile(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    audio = _archive_profile(profile).get("audio")
    return audio if isinstance(audio, Mapping) else {}


def _artifact_drop_reason_map(profile: Mapping[str, Any] | None) -> dict[str, str]:
    source = profile.get("source") if profile else None
    if source is None:
        return {}
    if not isinstance(source, Mapping):
        raise ValueError("source profile must be a mapping")
    drops = source.get("artifact_drops") or []
    if not isinstance(drops, Sequence) or isinstance(drops, (str, bytes)):
        raise ValueError("source artifact_drops must be a list")

    reasons: dict[str, str] = {}
    for item in drops:
        if not isinstance(item, Mapping):
            raise ValueError("source artifact drop entries must be mappings")
        selector = item.get("selector")
        reason = item.get("reason")
        if not isinstance(selector, str):
            raise ValueError("source artifact drop selector must be a string")
        if not isinstance(reason, str):
            raise ValueError("source artifact drop reason must be a string")
        normalized = normalize_artifact_drop_selector(selector)
        stripped_reason = reason.strip()
        if not stripped_reason:
            raise ValueError("source artifact drop reason must not be blank")
        if normalized in reasons:
            raise ValueError(f"duplicate source artifact drop selector: {normalized}")
        reasons[normalized] = stripped_reason
    return reasons


def _export_auxiliary_streams(source: pathlib.Path, exports: Sequence[Mapping[str, Any]]) -> None:
    for export in exports:
        spec = export.get("spec")
        if not isinstance(spec, str) or not spec:
            continue
        export_path = pathlib.Path(str(export["path"]))
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.unlink(missing_ok=True)

        stype = str(export.get("stype") or "")
        muxer = str(export.get("muxer") or "matroska")
        cmd = [
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
            *source_artifacts._metadata_copy_args([stype]),
            *source_artifacts.FFMPEG_OUTPUT_FLAGS,
            "-c",
            "copy",
            "-f",
            muxer,
            str(export_path),
        ]
        if muxer == "data":
            cmd = [
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
        _run_checked(cmd, f"source artifact export {source} stream {spec}")

        try:
            source_artifacts._repair_iso_bmff_stream_artifact_sample_entry(
                source,
                export_path,
                cast(dict[str, Any], export.get("stream", {})),
            )
        except Exception as exc:
            raise RuntimeError(
                f"failed to repair source stream artifact for {source} stream {spec}: {exc}"
            ) from exc

        if not export_path.exists():
            raise RuntimeError(f"expected source artifact export missing: {export_path}")


def _maybe_remux_matroska_data_tracks(
    source: pathlib.Path,
    archive_mkv: pathlib.Path,
    data_infos: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if not data_infos:
        return None
    mkvmerge = shutil.which("mkvmerge")
    if not mkvmerge:
        raise RuntimeError("mkvmerge is required to preserve Matroska/WebM data tracks")
    part = archive_mkv.with_name(f".{archive_mkv.name}.with-source-data.part")
    part.unlink(missing_ok=True)
    cmd = [
        mkvmerge,
        "-o",
        str(part),
        "--disable-track-statistics-tags",
        str(archive_mkv),
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
        _run_checked(cmd, "Matroska/WebM data track preservation remux")
        os.replace(part, archive_mkv)
    finally:
        part.unlink(missing_ok=True)
    return {
        "tool": "mkvmerge",
        "data_tracks_copied": len(data_infos),
        "command": cmd,
    }


def build_strict_source_artifacts(
    *,
    source: pathlib.Path,
    archive_mkv: pathlib.Path,
    encode_command: Sequence[str],
    encode_profile: Mapping[str, Any] | None,
    source_filesystem_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(source_filesystem_metadata, Mapping):
        raise RuntimeError(
            "unresumable: source filesystem metadata sidecar is missing for "
            f"{source.name}"
        )
    profile = dict(encode_profile or {})
    drop_policy = source_artifacts.SourceArtifactDropPolicy(_artifact_drop_reason_map(profile))
    work_dir = archive_mkv.parent / f".{archive_mkv.name}.source-artifacts-work"
    bundle_path = pathlib.Path(source_artifacts._source_artifacts_path(str(archive_mkv)))
    part_path = pathlib.Path(str(bundle_path) + ".part")
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    part_path.unlink(missing_ok=True)

    try:
        dumped = source_artifacts._dump_streams_and_metadata(
            str(source),
            work_dir,
            False,
            drop_policy=drop_policy,
            allow_conversion_only_container=False,
            naming_stem=source.stem,
        )
        exports = dumped["exports"]
        container_artifacts = dumped["container_artifacts"]
        container_inventory = dumped["container_inventory"]
        source_container = dumped["source_container"]
        dropped_items = dumped["dropped_items"]
        stream_infos = dumped["stream_infos"]

        source_container_mode = str(source_container.get("mode") or "")
        copy_matroska_data_tracks = source_container_mode in {"matroska_rebuild", "webm_rebuild"}
        dropped_stream_indices = source_artifacts._dropped_stream_indices(dropped_items)
        data_infos = [
            info
            for info in stream_infos
            if info["stype"] == "d"
            and copy_matroska_data_tracks
            and info["index"] not in dropped_stream_indices
        ]
        data_copy_specs = {
            spec
            for spec in (
                info.get("spec") if isinstance(info.get("spec"), str) else ""
                for info in data_infos
            )
            if spec
        }

        accounting_errors = source_artifacts._unaccounted_stream_errors(
            stream_infos,
            exports,
            dropped_items,
            data_copy_specs=data_copy_specs,
        )
        if accounting_errors:
            raise RuntimeError("; ".join(accounting_errors))

        _export_auxiliary_streams(source, exports)
        matroska_remux = _maybe_remux_matroska_data_tracks(source, archive_mkv, data_infos)

        def output_indices(stype: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for info in sorted(stream_infos, key=lambda item: item["index"]):
                spec = info.get("spec")
                if info["stype"] == stype and isinstance(spec, str) and spec:
                    result[spec] = len(result)
            return result

        data_output_indices = {
            str(info["spec"]): idx
            for idx, info in enumerate(data_infos)
            if isinstance(info.get("spec"), str) and info.get("spec")
        }
        stream_artifact_arcnames = source_artifacts._assign_source_stream_artifact_arcnames(
            container_artifacts,
            exports,
        )
        archive = _archive_profile(profile)
        audio = _audio_profile(profile)
        stream_transforms = source_artifacts._stream_transform_records(
            stream_infos,
            exports,
            dropped_items,
            video_output_indices=output_indices("v"),
            audio_output_indices=output_indices("a"),
            subtitle_output_indices=output_indices("s"),
            attachment_output_indices=output_indices("t"),
            data_output_indices=data_output_indices,
            src_video_copy=set(),
            src_audio_copy=set(),
            selected_output_path=archive_mkv,
            encode_output_path=archive_mkv,
            use_constant_quality=True,
            constant_quality=(
                archive.get("quality")
                if isinstance(archive.get("quality"), int)
                else None
            ),
            global_video_kbps=0,
            audio_kbps=_bitrate_kbps(audio.get("bitrate"), 128),
            svt_lp=0,
            video_codec=str(archive.get("codec") or "av1_nvenc"),
            video_encoder={
                key: value
                for key, value in archive.items()
                if key != "audio" and value is not None
            },
            audio_codec=str(audio.get("codec") or "libopus"),
            audio_encoder={key: value for key, value in audio.items() if value is not None},
            stream_artifact_arcnames=stream_artifact_arcnames,
        )

        source_metadata = dumped["source_metadata"]
        output_name = archive_mkv.name
        artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
            work_dir=work_dir,
            src=str(source),
            output=output_name,
            source_metadata=source_metadata,
            source_container=source_container,
            container_inventory=container_inventory,
            container_artifacts=container_artifacts,
            exports=exports,
            stream_transforms=stream_transforms,
            dropped_items=dropped_items,
            encode_cmd=list(encode_command),
            selected_output_path=archive_mkv,
            encode_output_path=archive_mkv,
            source_filesystem_metadata=source_filesystem_metadata,
        )

        created = source_artifacts._build_source_artifacts_bundle(
            part_path,
            artifacts,
            src=str(source),
            output=output_name,
            dropped_items=dropped_items,
        )
        if not created:
            raise RuntimeError(f"source artifact bundle was not created for {source}")

        audit = source_artifacts._audit_source_artifacts_bundle(part_path, archive_mkv=archive_mkv)
        errors = cast(list[str], audit.get("errors") or [])
        if errors:
            raise RuntimeError("source artifact audit failed: " + "; ".join(errors))
        if not audit.get("rebuild_supported"):
            warnings = cast(list[str], audit.get("warnings") or [])
            raise RuntimeError(
                "source artifacts do not support source-container rebuild"
                + (": " + "; ".join(warnings) if warnings else "")
            )

        os.replace(part_path, bundle_path)
        return {
            "output": str(bundle_path),
            "bytes": bundle_path.stat().st_size,
            "sha256": source_artifacts._sha256_path(bundle_path),
            "audit": audit,
            "matroska_remux": matroska_remux,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        part_path.unlink(missing_ok=True)
