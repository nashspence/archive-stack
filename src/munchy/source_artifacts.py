#!/usr/bin/env python3

"""Generic Munchy media-preservation and source-artifact implementation.

Munchy archive/review services use this module to preserve source-container
artifacts alongside re-encoded media.
"""

import hashlib
import io
import json
import logging
import mimetypes
import os
import pathlib
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from typing import (
    Any,
    NamedTuple,
    TypedDict,
    cast,
)

from munchy.profiles import normalize_artifact_drop_selector

SOURCE_ARTIFACTS_SUFFIX = ".source-artifacts.tar.zst"
SOURCE_ARTIFACTS_ZSTD_LEVEL = "19"
SOURCE_ARTIFACTS_ZSTD_LONG = "27"
DEFAULT_REBUILD_APPEND_TOP_LEVEL_ATOMS = ("uuid", "meta")
DEFAULT_REBUILD_GRAFT_MOOV_METADATA = True
MOOV_METADATA_CHILD_ATOMS = (b"udta", b"meta")

FFMPEG_INPUT_FLAGS = ["-fflags", "+genpts"]
FFMPEG_OUTPUT_FLAGS = [
    "-avoid_negative_ts",
    "make_zero",
    "-max_interleave_delta",
    "0",
]

VERBOSE_LEVEL = 0


class _StreamExportRequired(TypedDict):
    path: str
    stream: dict[str, Any]
    stype: str
    mkv_ok: bool


class StreamExport(_StreamExportRequired, total=False):
    packet_timestamps_path: str
    spec: str
    muxer: str


class StreamInfo(TypedDict):
    index: int
    stream: dict[str, Any]
    stype: str
    mkv_ok: bool
    spec: str


class SourceArtifact(NamedTuple):
    path: pathlib.Path
    arcname: str
    kind: str
    description: str
    mime_type: str
    metadata: dict[str, Any]


class DumpedStreams(TypedDict):
    exports: list[StreamExport]
    attachments: list[pathlib.Path]
    metadata_path: pathlib.Path | None
    source_metadata: dict[str, Any]
    source_container: dict[str, Any]
    container_artifacts: list[SourceArtifact]
    container_inventory: list[dict[str, Any]]
    dropped_items: list[dict[str, Any]]
    container_tags: dict[str, str]
    stream_infos: list[StreamInfo]


class TopLevelAtom(NamedTuple):
    offset: int
    size: int
    kind: bytes
    header_size: int


class SourceArtifactDropPolicy:
    def __init__(self, selectors: Sequence[str] | Mapping[str, str]) -> None:
        if isinstance(selectors, Mapping):
            self.reasons = {
                normalize_artifact_drop_selector(selector): str(reason).strip()
                for selector, reason in selectors.items()
            }
            self.selectors = list(self.reasons)
        else:
            self.selectors = [_normalize_drop_selector(selector) for selector in selectors]
            self.reasons = {selector: "user_requested" for selector in self.selectors}
        self.used: set[str] = set()
        for selector in self.selectors:
            if not _is_valid_drop_selector(selector):
                raise ValueError(
                    "invalid --drop-source-artifact selector "
                    f"{selector!r}; expected stream:N, atom:TYPE, "
                    "atom:TYPE:N, top-level-atom:TYPE, or atom-offset:OFFSET"
                )
            if not self.reasons.get(selector):
                raise ValueError(f"drop reason must not be blank for selector {selector!r}")

    def matches_stream(self, index: int) -> str | None:
        selector = f"stream:{index}"
        if selector in self.selectors:
            self.used.add(selector)
            return selector
        return None

    def matches_atom(self, atom: TopLevelAtom, type_ordinal: int) -> str | None:
        atom_type = _atom_type_label(atom.kind)
        candidates = [
            f"atom:{atom_type}",
            f"atom:{atom_type}:{type_ordinal}",
            f"top-level-atom:{atom_type}",
            f"top-level-atom:{atom_type}:{type_ordinal}",
            f"atom-offset:{atom.offset}",
            f"atom-offset:0x{atom.offset:x}",
        ]
        for candidate in candidates:
            if candidate in self.selectors:
                self.used.add(candidate)
                return candidate
        return None

    def reason_for(self, selector: str) -> str:
        return self.reasons.get(selector, "user_requested")


_METADATA_COPY_BASE = ["-map_metadata", "0"]
_METADATA_COPY_STREAM_MAP: list[tuple[str, list[str]]] = [
    ("v", ["-map_metadata:s:v", "0:s:v"]),
    ("a", ["-map_metadata:s:a", "0:s:a"]),
    ("s", ["-map_metadata:s:s", "0:s:s"]),
    ("d", ["-map_metadata:s:d", "0:s:d"]),
    ("t", ["-map_metadata:s:t", "0:s:t"]),
]


def _metadata_copy_args(stream_types: Sequence[str]) -> list[str]:
    args = list(_METADATA_COPY_BASE)
    present = {stype for stype in stream_types}
    for stype, option in _METADATA_COPY_STREAM_MAP:
        if stype in present:
            args.extend(option)
    return args


def _format_size_for_log(num_bytes: int) -> str:
    return (
        f"{num_bytes / float(1024**2):.2f} MiB ({num_bytes:,} bytes)"
        if num_bytes >= 0
        else f"{num_bytes:,} bytes"
    )


def ffprobe_json(cmd: Sequence[str]) -> dict[str, Any]:
    _print_command(cmd)
    proc = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
    )
    stdout = proc.stdout.decode("utf-8", "replace")
    if not stdout.strip():
        return {}
    return cast(dict[str, Any], json.loads(stdout))


def now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


VIDEO_STREAM_MAP: dict[str, tuple[str, str, bool]] = {
    "h264": ("h264", "h264", True),
    "hevc": ("hevc", "h265", True),
    "mpeg4": ("m4v", "m4v", True),
    "mpeg2video": ("mpeg2video", "m2v", True),
    "vp9": ("ivf", "ivf", True),
    "av1": ("ivf", "ivf", True),
    "mjpeg": ("mjpeg", "mjpeg", False),
    "png": ("image2", "png", False),
    "bmp": ("image2", "bmp", False),
    "webp": ("image2", "webp", False),
}


AUDIO_STREAM_MAP: dict[str, tuple[str, str, bool]] = {
    "aac": ("adts", "aac", True),
    "ac3": ("ac3", "ac3", True),
    "eac3": ("eac3", "eac3", True),
    "mp3": ("mp3", "mp3", True),
    "flac": ("flac", "flac", True),
    "opus": ("opus", "opus", True),
    "vorbis": ("ogg", "ogg", True),
    "pcm_s16le": ("wav", "wav", True),
    "pcm_s24le": ("wav", "wav", True),
    "pcm_s32le": ("wav", "wav", True),
}


SUBTITLE_STREAM_MAP: dict[str, tuple[str, str, bool]] = {
    "subrip": ("srt", "srt", True),
    "srt": ("srt", "srt", True),
    "ass": ("ass", "ass", True),
    "ssa": ("ass", "ass", True),
    "webvtt": ("webvtt", "vtt", True),
    "hdmv_pgs_subtitle": ("sup", "sup", True),
}


RAW_STREAM_DUMP = ("data", "bin", False)


_EXTENSION_OVERRIDES = {
    "matroska": "mkv",
    "quicktime": "mov",
}


_ISO_BMFF_CONTAINER_SUFFIXES = {
    ".3g2",
    ".3gp",
    ".heic",
    ".heif",
    ".m4a",
    ".m4v",
    ".mov",
    ".mp4",
    ".qt",
}

_ISO_BMFF_FORMAT_NAMES = {
    "3g2",
    "3gp",
    "heic",
    "heif",
    "m4a",
    "m4v",
    "mj2",
    "mov",
    "mp4",
    "quicktime",
}

_MATROSKA_CONTAINER_SUFFIXES = {
    ".mka",
    ".mk3d",
    ".mks",
    ".mkv",
    ".webm",
}

_MATROSKA_FORMAT_NAMES = {
    "matroska",
    "webm",
}


def _normalize_component(value: str | None, fallback: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        text = fallback
    cleaned = re.sub(r"[^0-9a-z]+", "_", text)
    cleaned = cleaned.strip("_")
    if not cleaned:
        return fallback
    return cleaned


def _build_stream_identifier(stype: str, index: int, stream: dict[str, Any]) -> str:
    kind = "s" if stype == "s" else "d"
    inferred_type = "subtitle" if kind == "s" else "data"
    codec_type = _normalize_component(stream.get("codec_type"), inferred_type)
    codec_tag = _normalize_component(stream.get("codec_tag_string"), "unknown")
    return f"{kind}{index}.{codec_type}.{codec_tag}"


def _select_extension(*candidates: str | None) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        text = str(candidate).strip().lower()
        if not text:
            continue
        text = text.split(",")[0].strip()
        if not text:
            continue
        override = _EXTENSION_OVERRIDES.get(text)
        if override:
            return override
        cleaned = re.sub(r"[^0-9a-z]+", "", text)
        if cleaned:
            return cleaned
    return "bin"


def _metadata_format_name(metadata: dict[str, Any]) -> str:
    fmt_obj = metadata.get("format")
    if not isinstance(fmt_obj, dict):
        return ""
    raw_format_name = fmt_obj.get("format_name")
    return raw_format_name if isinstance(raw_format_name, str) else ""


def _metadata_format_names(metadata: dict[str, Any]) -> set[str]:
    return {
        name.strip().lower() for name in _metadata_format_name(metadata).split(",") if name.strip()
    }


def _is_iso_bmff_source(
    source_path: pathlib.Path,
    metadata: dict[str, Any] | None = None,
) -> bool:
    if source_path.suffix.lower() in _ISO_BMFF_CONTAINER_SUFFIXES:
        return True
    if metadata is None:
        return False
    return bool(_metadata_format_names(metadata) & _ISO_BMFF_FORMAT_NAMES)


def _is_matroska_source(
    source_path: pathlib.Path,
    metadata: dict[str, Any] | None = None,
) -> bool:
    if source_path.suffix.lower() in _MATROSKA_CONTAINER_SUFFIXES:
        return True
    if metadata is None:
        return False
    return bool(_metadata_format_names(metadata) & _MATROSKA_FORMAT_NAMES)


def _matroska_rebuild_mode(
    source_path: pathlib.Path,
    metadata: dict[str, Any],
) -> str:
    suffix = source_path.suffix.lower()
    if suffix == ".webm":
        return "webm_rebuild"
    if suffix in _MATROSKA_CONTAINER_SUFFIXES:
        return "matroska_rebuild"
    format_names = _metadata_format_names(metadata)
    if "webm" in format_names and "matroska" not in format_names:
        return "webm_rebuild"
    return "matroska_rebuild"


def _normalize_extension(ext: str | None) -> str:
    text = str(ext or "").strip().lower()
    text = text.lstrip(".")
    text = re.sub(r"[^0-9a-z]+", "", text)
    if not text:
        return "data"
    return text


def _build_stream_attachment_name(
    stype: str, index: int, stream: dict[str, Any], extension: str
) -> str:
    identifier = _build_stream_identifier(stype, index, stream)
    normalized_ext = _normalize_extension(extension)
    return f"source_stream_{identifier}.{normalized_ext}"


def _atom_type_label(kind: bytes) -> str:
    try:
        text = kind.decode("ascii")
    except UnicodeDecodeError:
        text = kind.hex()
    cleaned = re.sub(r"[^0-9a-z]+", "_", text.lower()).strip("_")
    return cleaned or kind.hex()


def _normalize_drop_selector(selector: str) -> str:
    return re.sub(r"\s+", "", str(selector or "").strip().lower())


def _is_valid_drop_selector(selector: str) -> bool:
    if re.fullmatch(r"stream:\d+", selector):
        return True
    if re.fullmatch(r"(atom|top-level-atom):[0-9a-z_]+(:\d+)?", selector):
        return True
    if re.fullmatch(r"atom-offset:(0x[0-9a-f]+|\d+)", selector):
        return True
    return False


def _iter_top_level_atoms(path: pathlib.Path) -> list[TopLevelAtom]:
    file_size = path.stat().st_size
    atoms: list[TopLevelAtom] = []
    with path.open("rb") as fh:
        offset = 0
        while offset + 8 <= file_size:
            fh.seek(offset)
            header = fh.read(8)
            if len(header) != 8:
                break
            size32, kind = struct.unpack(">I4s", header)
            header_size = 8
            if size32 == 1:
                extended_size = fh.read(8)
                if len(extended_size) != 8:
                    raise ValueError(f"truncated extended atom header at {offset}")
                size = struct.unpack(">Q", extended_size)[0]
                header_size = 16
            elif size32 == 0:
                size = file_size - offset
            else:
                size = size32
            if size < header_size:
                raise ValueError(f"invalid top-level atom {kind!r} at {offset}: size {size}")
            if offset + size > file_size:
                raise ValueError(f"top-level atom {kind!r} at {offset} extends past EOF")
            atoms.append(TopLevelAtom(offset, size, kind, header_size))
            offset += size
        if offset != file_size:
            raise ValueError(f"unparsed trailing bytes after offset {offset}")
    return atoms


def _iter_atoms_in_bytes(data: bytes | bytearray, start: int, end: int) -> list[TopLevelAtom]:
    atoms: list[TopLevelAtom] = []
    offset = start
    while offset + 8 <= end:
        size32, kind = struct.unpack(">I4s", data[offset : offset + 8])
        header_size = 8
        if size32 == 1:
            if offset + 16 > end:
                raise ValueError(f"truncated extended atom header at {offset}")
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header_size = 16
        elif size32 == 0:
            size = end - offset
        else:
            size = size32
        if size < header_size:
            raise ValueError(f"invalid atom {kind!r} at {offset}: size {size}")
        if offset + size > end:
            raise ValueError(f"atom {kind!r} at {offset} extends past parent")
        atoms.append(TopLevelAtom(offset, size, kind, header_size))
        offset += size
    if offset != end:
        raise ValueError(f"unparsed trailing bytes after offset {offset}")
    return atoms


def _find_child_atom(
    data: bytes | bytearray,
    parent: TopLevelAtom,
    kind: bytes,
) -> TopLevelAtom:
    for atom in _iter_atoms_in_bytes(
        data,
        parent.offset + parent.header_size,
        parent.offset + parent.size,
    ):
        if atom.kind == kind:
            return atom
    raise ValueError(f"missing child atom {kind!r} under {parent.kind!r}")


def _find_atom_path(data: bytes | bytearray, path: Sequence[bytes]) -> TopLevelAtom:
    if not path:
        raise ValueError("atom path must not be empty")
    atoms = _iter_atoms_in_bytes(data, 0, len(data))
    found: TopLevelAtom | None = None
    for kind in path:
        found = next((atom for atom in atoms if atom.kind == kind), None)
        if found is None:
            readable = "/".join(part.decode("ascii", "replace") for part in path)
            raise ValueError(f"missing atom path {readable}")
        atoms = _iter_atoms_in_bytes(
            data,
            found.offset + found.header_size,
            found.offset + found.size,
        )
    if found is None:
        raise ValueError("atom path must not be empty")
    return found


def _stsd_entries(data: bytes | bytearray, stsd: TopLevelAtom) -> tuple[int, bytes]:
    if stsd.size < stsd.header_size + 8:
        raise ValueError("stsd atom is too small")
    entry_count_offset = stsd.offset + stsd.header_size + 4
    entries_offset = entry_count_offset + 4
    entry_count = struct.unpack(">I", data[entry_count_offset:entries_offset])[0]
    return entry_count, bytes(data[entries_offset : stsd.offset + stsd.size])


def _set_atom_size(data: bytearray, atom: TopLevelAtom, size: int) -> None:
    if atom.header_size != 8:
        raise ValueError("extended-size atoms are not supported for stream artifact repair")
    data[atom.offset : atom.offset + 4] = struct.pack(">I", size)


def _atom_payload_bounds(atom: TopLevelAtom) -> tuple[int, int]:
    return atom.offset + atom.header_size, atom.offset + atom.size


def _single_full_atom(data: bytes | bytearray, kind: bytes) -> TopLevelAtom:
    atoms = _iter_atoms_in_bytes(data, 0, len(data))
    if len(atoms) != 1 or atoms[0].kind != kind:
        readable = kind.decode("ascii", "replace")
        raise ValueError(f"expected exactly one {readable} atom")
    return atoms[0]


def _set_full_atom_size(data: bytearray, atom: TopLevelAtom, size: int) -> None:
    if atom.header_size != 8:
        raise ValueError("extended-size atoms are not supported for moov grafting")
    data[atom.offset : atom.offset + 4] = struct.pack(">I", size)


def _graft_moov_metadata_atom(
    target_moov: bytes,
    source_moov: bytes,
    *,
    metadata_child_kinds: Sequence[bytes] = MOOV_METADATA_CHILD_ATOMS,
) -> tuple[bytes, list[str]]:
    target_atom = _single_full_atom(target_moov, b"moov")
    source_atom = _single_full_atom(source_moov, b"moov")
    if target_atom.header_size != 8 or source_atom.header_size != 8:
        raise RuntimeError("extended-size moov atoms are not supported for metadata grafting")

    graft_kinds = set(metadata_child_kinds)
    source_children = _iter_atoms_in_bytes(
        source_moov,
        *_atom_payload_bounds(source_atom),
    )
    source_metadata = [
        bytes(source_moov[child.offset : child.offset + child.size])
        for child in source_children
        if child.kind in graft_kinds
    ]

    target_children = _iter_atoms_in_bytes(
        target_moov,
        *_atom_payload_bounds(target_atom),
    )
    grafted_labels = [
        child.kind.decode("ascii", "replace")
        for child in source_children
        if child.kind in graft_kinds
    ]

    if source_metadata:
        payload = bytearray()
        inserted = False
        for child in target_children:
            if child.kind in graft_kinds:
                if not inserted:
                    for child_bytes in source_metadata:
                        payload.extend(child_bytes)
                    inserted = True
                continue
            payload.extend(target_moov[child.offset : child.offset + child.size])
        if not inserted:
            for child_bytes in source_metadata:
                payload.extend(child_bytes)

        grafted = bytearray(target_moov[: target_atom.header_size])
        grafted.extend(payload)
        _set_full_atom_size(
            grafted,
            TopLevelAtom(0, len(grafted), b"moov", 8),
            len(grafted),
        )
    else:
        grafted = bytearray(target_moov)

    if _copy_full_box_creation_modification_times(
        grafted,
        source_moov,
        [b"moov", b"mvhd"],
    ):
        grafted_labels.append("mvhd.times")

    if not grafted_labels:
        return target_moov, []
    return bytes(grafted), grafted_labels


def _copy_full_box_creation_modification_times(
    target: bytearray,
    source: bytes | bytearray,
    path: Sequence[bytes],
) -> bool:
    try:
        target_atom = _find_atom_path(target, path)
        source_atom = _find_atom_path(source, path)
    except ValueError:
        return False
    if target_atom.size < target_atom.header_size + 12:
        return False
    if source_atom.size < source_atom.header_size + 12:
        return False

    target_version = target[target_atom.offset + target_atom.header_size]
    source_version = source[source_atom.offset + source_atom.header_size]
    if target_version != source_version:
        return False
    if source_version == 0:
        field_size = 8
    elif source_version == 1:
        field_size = 16
    else:
        return False

    source_start = source_atom.offset + source_atom.header_size + 4
    target_start = target_atom.offset + target_atom.header_size + 4
    if source_start + field_size > source_atom.offset + source_atom.size:
        return False
    if target_start + field_size > target_atom.offset + target_atom.size:
        return False
    target[target_start : target_start + field_size] = source[
        source_start : source_start + field_size
    ]
    return True


def _adjust_chunk_offsets_for_moov_delta(
    moov: bytearray,
    *,
    shifted_file_region_start: int,
    delta: int,
) -> int:
    if delta == 0:
        return 0

    adjusted = 0
    container_kinds = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}

    def walk(start: int, end: int) -> None:
        nonlocal adjusted
        for atom in _iter_atoms_in_bytes(moov, start, end):
            if atom.kind == b"stco":
                if atom.size < atom.header_size + 8:
                    raise RuntimeError("stco atom is too small")
                count_offset = atom.offset + atom.header_size + 4
                count = struct.unpack(">I", moov[count_offset : count_offset + 4])[0]
                entries_offset = count_offset + 4
                entries_end = entries_offset + (count * 4)
                if entries_end > atom.offset + atom.size:
                    raise RuntimeError("stco atom extends past its declared size")
                for index in range(count):
                    offset = entries_offset + (index * 4)
                    value = struct.unpack(">I", moov[offset : offset + 4])[0]
                    if value < shifted_file_region_start:
                        continue
                    new_value = value + delta
                    if new_value < 0 or new_value > 0xFFFFFFFF:
                        raise RuntimeError(
                            "stco chunk offset cannot be adjusted without converting to co64"
                        )
                    moov[offset : offset + 4] = struct.pack(">I", new_value)
                    adjusted += 1
            elif atom.kind == b"co64":
                if atom.size < atom.header_size + 8:
                    raise RuntimeError("co64 atom is too small")
                count_offset = atom.offset + atom.header_size + 4
                count = struct.unpack(">I", moov[count_offset : count_offset + 4])[0]
                entries_offset = count_offset + 4
                entries_end = entries_offset + (count * 8)
                if entries_end > atom.offset + atom.size:
                    raise RuntimeError("co64 atom extends past its declared size")
                for index in range(count):
                    offset = entries_offset + (index * 8)
                    value = struct.unpack(">Q", moov[offset : offset + 8])[0]
                    if value < shifted_file_region_start:
                        continue
                    new_value = value + delta
                    if new_value < 0:
                        raise RuntimeError("co64 chunk offset became negative")
                    moov[offset : offset + 8] = struct.pack(">Q", new_value)
                    adjusted += 1
            elif atom.kind in container_kinds:
                walk(atom.offset + atom.header_size, atom.offset + atom.size)

    root = _single_full_atom(moov, b"moov")
    walk(root.offset + root.header_size, root.offset + root.size)
    return adjusted


def _graft_moov_metadata_into_file_bytes(
    target_file: bytes,
    source_moov: bytes,
) -> tuple[bytes, dict[str, Any]]:
    target_moov = _find_atom_path(target_file, [b"moov"])
    original_moov_bytes = bytes(
        target_file[target_moov.offset : target_moov.offset + target_moov.size]
    )
    grafted_moov, grafted_atoms = _graft_moov_metadata_atom(
        original_moov_bytes,
        source_moov,
    )
    if not grafted_atoms:
        return target_file, {
            "grafted_atoms": [],
            "chunk_offsets_adjusted": 0,
            "moov_size_delta": 0,
        }

    delta = len(grafted_moov) - target_moov.size
    grafted_moov_data = bytearray(grafted_moov)
    adjusted_offsets = _adjust_chunk_offsets_for_moov_delta(
        grafted_moov_data,
        shifted_file_region_start=target_moov.offset + target_moov.size,
        delta=delta,
    )
    rebuilt = (
        target_file[: target_moov.offset]
        + bytes(grafted_moov_data)
        + target_file[target_moov.offset + target_moov.size :]
    )
    return rebuilt, {
        "grafted_atoms": grafted_atoms,
        "chunk_offsets_adjusted": adjusted_offsets,
        "moov_size_delta": delta,
    }


def _graft_moov_metadata_into_file(
    target_path: pathlib.Path,
    source_moov_path: pathlib.Path,
    output_path: pathlib.Path,
) -> dict[str, Any]:
    rebuilt, result = _graft_moov_metadata_into_file_bytes(
        target_path.read_bytes(),
        source_moov_path.read_bytes(),
    )
    if not result.get("grafted_atoms"):
        return result
    output_path.write_bytes(rebuilt)
    return result


def _repair_iso_bmff_stream_artifact_sample_entry(
    source: pathlib.Path,
    artifact: pathlib.Path,
    stream: dict[str, Any],
) -> None:
    if source.suffix.lower() not in _ISO_BMFF_CONTAINER_SUFFIXES:
        return
    if artifact.suffix.lower() not in _ISO_BMFF_CONTAINER_SUFFIXES:
        return
    try:
        stream_index = int(cast(Any, stream.get("index")))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stream artifact has no numeric source stream index") from exc

    source_data = source.read_bytes()
    source_moov = _find_atom_path(source_data, [b"moov"])
    source_tracks = [
        atom
        for atom in _iter_atoms_in_bytes(
            source_data,
            source_moov.offset + source_moov.header_size,
            source_moov.offset + source_moov.size,
        )
        if atom.kind == b"trak"
    ]
    if stream_index >= len(source_tracks):
        raise RuntimeError(f"source stream index {stream_index} has no matching MP4 track")
    source_track = source_tracks[stream_index]
    source_mdia = _find_child_atom(source_data, source_track, b"mdia")
    source_hdlr = _find_child_atom(source_data, source_mdia, b"hdlr")
    source_minf = _find_child_atom(source_data, source_mdia, b"minf")
    source_stbl = _find_child_atom(source_data, source_minf, b"stbl")
    source_stsd = _find_child_atom(source_data, source_stbl, b"stsd")
    source_entry_count, source_entries = _stsd_entries(source_data, source_stsd)
    if source_entry_count <= 0 or not source_entries:
        raise RuntimeError(f"source stream {stream_index} has no stsd sample entry")

    artifact_data = bytearray(artifact.read_bytes())
    target_moov = _find_atom_path(artifact_data, [b"moov"])
    target_trak = _find_child_atom(artifact_data, target_moov, b"trak")
    target_mdia = _find_child_atom(artifact_data, target_trak, b"mdia")
    target_hdlr = _find_child_atom(artifact_data, target_mdia, b"hdlr")
    target_minf = _find_child_atom(artifact_data, target_mdia, b"minf")
    target_stbl = _find_child_atom(artifact_data, target_minf, b"stbl")
    target_stsd = _find_child_atom(artifact_data, target_stbl, b"stsd")

    if source_hdlr.size >= 24 and target_hdlr.size >= 24:
        artifact_data[target_hdlr.offset + 16 : target_hdlr.offset + 20] = source_data[
            source_hdlr.offset + 16 : source_hdlr.offset + 20
        ]

    old_stsd = bytes(artifact_data[target_stsd.offset : target_stsd.offset + target_stsd.size])
    new_stsd = old_stsd[: target_stsd.header_size + 4]
    new_stsd += struct.pack(">I", source_entry_count)
    new_stsd += source_entries
    delta = len(new_stsd) - len(old_stsd)
    if delta == 0 and old_stsd == new_stsd:
        return

    artifact_data[target_stsd.offset : target_stsd.offset + target_stsd.size] = new_stsd
    for atom in (
        target_moov,
        target_trak,
        target_mdia,
        target_minf,
        target_stbl,
        target_stsd,
    ):
        _set_atom_size(artifact_data, atom, atom.size + delta)

    temp_path = artifact.with_name(artifact.name + ".repair")
    temp_path.write_bytes(artifact_data)
    temp_path.replace(artifact)


def _source_drop_record(
    *,
    item_id: str,
    kind: str,
    selector: str,
    reason: str = "user_requested",
    description: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": item_id,
        "kind": kind,
        "action": "intentionally_dropped",
        "selector": selector,
        "reason": reason,
        "description": description,
        **metadata,
    }


def _copy_atom_bytes(
    source: pathlib.Path,
    output: pathlib.Path,
    atom: TopLevelAtom,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    remaining = atom.size
    with source.open("rb") as src_fh, output.open("wb") as out_fh:
        src_fh.seek(atom.offset)
        while remaining:
            chunk = src_fh.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError(f"unexpected EOF while copying atom {atom.kind!r} from {source}")
            out_fh.write(chunk)
            remaining -= len(chunk)


def _export_top_level_container_atoms(
    src: str,
    dest_dir: pathlib.Path,
    drop_policy: SourceArtifactDropPolicy,
    *,
    is_iso_bmff: bool | None = None,
) -> tuple[list[dict[str, Any]], list[SourceArtifact], list[dict[str, Any]]]:
    source_path = pathlib.Path(src)
    if is_iso_bmff is None:
        is_iso_bmff = _is_iso_bmff_source(source_path)
    if not is_iso_bmff:
        return [], [], []

    atoms = _iter_top_level_atoms(source_path)

    inventory: list[dict[str, Any]] = []
    artifacts: list[SourceArtifact] = []
    dropped: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    for atom in atoms:
        atom_label = _atom_type_label(atom.kind)
        type_counts[atom_label] = type_counts.get(atom_label, 0) + 1
        type_ordinal = type_counts[atom_label]
        item_id = f"top-level-atom:{atom_label}:{type_ordinal}"
        base_metadata = {
            "atom_type": atom.kind.decode("ascii", "replace"),
            "atom_offset": atom.offset,
            "atom_size": atom.size,
            "atom_header_size": atom.header_size,
            "atom_ordinal": type_ordinal,
        }

        if atom.kind == b"mdat":
            inventory.append(
                {
                    "id": item_id,
                    "kind": "top_level_atom",
                    "action": "accounted_by_stream_transforms",
                    "description": "Source media data atom; represented by stream transforms",
                    **base_metadata,
                }
            )
            continue

        drop_selector = drop_policy.matches_atom(atom, type_ordinal)
        if drop_selector:
            record = _source_drop_record(
                item_id=item_id,
                kind="top_level_atom",
                selector=drop_selector,
                reason=drop_policy.reason_for(drop_selector),
                description="Top-level ISO BMFF/QuickTime atom",
                metadata=base_metadata,
            )
            inventory.append(record)
            dropped.append(record)
            continue

        file_name = f"top-level-{atom_label}-{type_ordinal:02d}.offset-{atom.offset:016x}.atom"
        artifact_path = dest_dir / "container_atoms" / file_name
        _copy_atom_bytes(source_path, artifact_path, atom)
        arcname = f"container/{file_name}"
        inventory.append(
            {
                "id": item_id,
                "kind": "top_level_atom",
                "action": "preserved_as_source_artifact",
                "description": "Top-level ISO BMFF/QuickTime atom",
                "artifact": arcname,
                "preservation": "raw_full_atom",
                **base_metadata,
            }
        )
        artifacts.append(
            SourceArtifact(
                artifact_path,
                arcname,
                "top_level_container_atom",
                "Top-level ISO BMFF/QuickTime atom",
                "application/octet-stream",
                {
                    **base_metadata,
                    "preservation": "raw_full_atom",
                },
            )
        )
    return inventory, artifacts, dropped


def _export_matroska_container_artifacts(
    src: str,
    dest_dir: pathlib.Path,
) -> tuple[list[dict[str, Any]], list[SourceArtifact]]:
    mkvmerge = shutil.which("mkvmerge")
    if not mkvmerge:
        raise RuntimeError("mkvmerge is required for Matroska/WebM source-container support")

    proc = subprocess.run(
        [mkvmerge, "-J", src],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (
            proc.stderr.decode("utf-8", "replace").strip()
            or proc.stdout.decode("utf-8", "replace").strip()
            or f"exit code {proc.returncode}"
        )
        raise RuntimeError(f"mkvmerge source identification failed: {detail}")

    try:
        identification = json.loads(proc.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid mkvmerge source identification JSON: {exc}") from exc

    artifact_path = dest_dir / "container" / "matroska-identification.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(identification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    arcname = "container/matroska-identification.json"
    track_count = len(identification.get("tracks") or [])
    attachment_count = len(identification.get("attachments") or [])
    inventory = [
        {
            "id": "matroska:identification",
            "kind": "matroska_container",
            "action": "preserved_as_source_artifact",
            "description": "mkvmerge Matroska/WebM source identification",
            "artifact": arcname,
            "preservation": "structured_mkvmerge_json",
            "tracks": track_count,
            "attachments": attachment_count,
        }
    ]
    artifacts = [
        SourceArtifact(
            artifact_path,
            arcname,
            "matroska_identification",
            "mkvmerge Matroska/WebM source identification",
            "application/json",
            {
                "preservation": "structured_mkvmerge_json",
                "tracks": track_count,
                "attachments": attachment_count,
            },
        )
    ]
    return inventory, artifacts


def _source_container_rebuild_contract(
    src: str,
    metadata: dict[str, Any],
    *,
    is_iso_bmff: bool,
    is_matroska: bool,
    allow_conversion_only_container: bool,
) -> dict[str, Any]:
    source_path = pathlib.Path(src)
    format_name = _metadata_format_name(metadata)
    suffix = source_path.suffix.lower()
    base = {
        "source_suffix": suffix,
        "format_name": format_name,
    }
    if is_iso_bmff:
        return {
            **base,
            "supported": True,
            "mode": "iso_bmff_rebuild",
            "preservation": (
                "top-level ISO BMFF/QuickTime atoms plus rebuild-muxable source stream artifacts"
            ),
        }
    if is_matroska:
        mode = _matroska_rebuild_mode(source_path, metadata)
        return {
            **base,
            "supported": True,
            "mode": mode,
            "preservation": (
                "Matroska/WebM tracks copied or re-encoded into the archive "
                "MKV plus mkvmerge source identification"
            ),
        }

    message = (
        "source container is not currently rebuild-supported "
        f"(suffix={suffix or '<none>'}, format={format_name or '<unknown>'}); "
        "munchy will not proceed because hidden container metadata or private "
        "streams could otherwise be lost silently"
    )
    if not allow_conversion_only_container:
        raise RuntimeError(
            message + "; use --allow-conversion-only-container only when you explicitly "
            "accept conversion-only archival semantics for this source"
        )
    return {
        **base,
        "supported": False,
        "mode": "conversion_only",
        "override": "allow_conversion_only_container",
        "message": message,
    }


def _export_top_level_meta_atoms(
    src: str,
    dest_dir: pathlib.Path,
) -> list[SourceArtifact]:
    _inventory, artifacts, _dropped = _export_top_level_container_atoms(
        src,
        dest_dir,
        SourceArtifactDropPolicy([]),
    )
    return [artifact for artifact in artifacts if artifact.metadata.get("atom_type") == "meta"]


def _classify_stream(stream: dict[str, Any]) -> tuple[str, tuple[str, str, bool]]:
    codec_type = cast(str, stream.get("codec_type") or "")
    codec_name = cast(str, (stream.get("codec_name") or "").lower())
    if codec_type == "video":
        return "v", VIDEO_STREAM_MAP.get(codec_name, RAW_STREAM_DUMP)
    if codec_type == "audio":
        return "a", AUDIO_STREAM_MAP.get(codec_name, RAW_STREAM_DUMP)
    if codec_type == "subtitle":
        return "s", SUBTITLE_STREAM_MAP.get(codec_name, RAW_STREAM_DUMP)
    if codec_type == "attachment":
        return "t", RAW_STREAM_DUMP
    return "d", RAW_STREAM_DUMP


def _parse_time_value(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            if "/" in value:
                try:
                    return float(Fraction(value))
                except (ValueError, ZeroDivisionError):
                    return None
    return None


def _collect_packet_timestamps_seconds(
    src: str, stream_index: int, stream_spec: str
) -> list[float] | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        stream_spec,
        "-show_packets",
        "-show_entries",
        "packet=stream_index,pts_time,dts_time,pos,flags",
        "-of",
        "json",
        src,
    ]
    try:
        data = ffprobe_json(cmd)
    except subprocess.CalledProcessError as exc:
        logging.debug(
            "ffprobe -show_packets failed for %s stream %s (%s): %s",
            src,
            stream_index,
            stream_spec,
            exc,
        )
        return None
    packets = cast(list[dict[str, Any]], data.get("packets") or [])
    timestamps: list[float] = []
    for packet in packets:
        value = _parse_time_value(packet.get("pts_time"))
        if value is None:
            value = _parse_time_value(packet.get("dts_time"))
        if value is None:
            continue
        timestamps.append(value)
    if not timestamps:
        logging.debug(
            "no packet timestamps found for %s stream %s (%s)",
            src,
            stream_index,
            stream_spec,
        )
        return []
    fixed: list[float] = []
    last = float("-inf")
    for ts in timestamps:
        if ts < last:
            ts = last
        fixed.append(ts)
        last = ts
    logging.debug(
        "collected %d packet timestamps for %s stream %s (%s)",
        len(fixed),
        src,
        stream_index,
        stream_spec,
    )
    return fixed


def _dump_streams_and_metadata(
    src: str,
    dest_dir: pathlib.Path,
    verbose: bool,
    *,
    drop_policy: SourceArtifactDropPolicy,
    allow_conversion_only_container: bool = False,
    naming_stem: str | None = None,
) -> DumpedStreams:
    dest_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_programs",
        "-show_chapters",
        src,
    ]
    metadata = ffprobe_json(cmd)
    source_path = pathlib.Path(src)
    source_suffix = source_path.suffix
    data_ext_hint = source_suffix[1:] if source_suffix.startswith(".") else ""
    container_format_name: str | None = None
    meta_path: pathlib.Path | None = None
    container_tags: dict[str, str] = {}
    if metadata:
        meta_path = dest_dir / "source-ffprobe.json"
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2)
            fh.write("\n")
        fmt_obj = metadata.get("format")
        if isinstance(fmt_obj, dict):
            raw_format_name = fmt_obj.get("format_name")
            if isinstance(raw_format_name, str):
                first_format = raw_format_name.split(",")[0].strip()
                if first_format:
                    container_format_name = first_format
            raw_tags = fmt_obj.get("tags")
            if isinstance(raw_tags, dict):
                for key, value in raw_tags.items():
                    if isinstance(key, str) and isinstance(value, str):
                        container_tags[key] = value

    source_is_iso_bmff = _is_iso_bmff_source(source_path, metadata)
    source_is_matroska = _is_matroska_source(source_path, metadata)
    source_container = _source_container_rebuild_contract(
        src,
        metadata,
        is_iso_bmff=source_is_iso_bmff,
        is_matroska=source_is_matroska,
        allow_conversion_only_container=allow_conversion_only_container,
    )

    exports: list[StreamExport] = []
    container_inventory, container_artifacts, dropped_items = _export_top_level_container_atoms(
        src,
        dest_dir,
        drop_policy,
        is_iso_bmff=source_is_iso_bmff,
    )
    if source_is_matroska:
        matroska_inventory, matroska_artifacts = _export_matroska_container_artifacts(
            src,
            dest_dir,
        )
        container_inventory.extend(matroska_inventory)
        container_artifacts.extend(matroska_artifacts)
    stream_infos: list[StreamInfo] = []
    streams = cast(list[dict[str, Any]], metadata.get("streams") or [])
    type_map = {
        "video": "v",
        "audio": "a",
        "subtitle": "s",
        "data": "d",
        "attachment": "t",
    }
    type_counters: dict[str, int] = {}
    stream_specifiers: dict[int, str] = {}
    for raw_stream in streams:
        try:
            raw_index = int(raw_stream.get("index", -1))
        except (TypeError, ValueError):
            continue
        letter = type_map.get(cast(str, raw_stream.get("codec_type") or ""))
        if not letter:
            continue
        ordinal = type_counters.get(letter, 0)
        type_counters[letter] = ordinal + 1
        stream_specifiers[raw_index] = f"{letter}:{ordinal}"

    for stream in streams:
        try:
            index = int(stream.get("index", -1))
        except (TypeError, ValueError):
            continue
        stype, (muxer, ext, mkv_ok) = _classify_stream(stream)
        spec = stream_specifiers.get(index, "")
        stream_infos.append(
            {
                "index": index,
                "stream": stream,
                "stype": stype,
                "mkv_ok": mkv_ok,
                "spec": spec,
            }
        )
        if stype == "t":
            continue
        if stype in {"v", "a"}:
            continue
        if stype == "s" and mkv_ok:
            continue
        if stype == "d" and source_is_matroska:
            drop_selector = drop_policy.matches_stream(index)
            if drop_selector:
                record = _source_drop_record(
                    item_id=f"stream:{index}",
                    kind="stream",
                    selector=drop_selector,
                    reason=drop_policy.reason_for(drop_selector),
                    description="Matroska/WebM source data track",
                    metadata={
                        "stream_index": index,
                        "stream_type": stype,
                        "stream_spec": spec,
                        "source_stream": stream,
                    },
                )
                dropped_items.append(record)
            continue
        drop_selector = drop_policy.matches_stream(index)
        if drop_selector:
            record = _source_drop_record(
                item_id=f"stream:{index}",
                kind="stream",
                selector=drop_selector,
                reason=drop_policy.reason_for(drop_selector),
                description="Source stream artifact preservation",
                metadata={
                    "stream_index": index,
                    "stream_type": stype,
                    "stream_spec": spec,
                    "source_stream": stream,
                },
            )
            dropped_items.append(record)
            continue
        if not spec:
            continue
        target_muxer = muxer
        primary_extension = ext
        if stype == "d":
            target_muxer = container_format_name or "data"
            if target_muxer == "matroska":
                target_muxer = "data"
            if target_muxer == "data":
                primary_extension = "data"
            else:
                primary_extension = _select_extension(
                    data_ext_hint,
                    container_format_name,
                    target_muxer,
                    primary_extension,
                )
        elif stype == "s" and not mkv_ok:
            if container_format_name:
                target_muxer = container_format_name
            primary_extension = _select_extension(
                container_format_name,
                data_ext_hint,
                primary_extension,
            )
        sidecar_name = _build_stream_attachment_name(stype, index, stream, primary_extension)
        sidecar = dest_dir / sidecar_name
        export_entry: StreamExport = {
            "path": str(sidecar),
            "stream": stream,
            "stype": stype,
            "mkv_ok": mkv_ok,
            "spec": spec,
            "muxer": target_muxer,
        }
        if stype == "d" and target_muxer == "data":
            stream_spec = stream_specifiers.get(index)
            timestamps: list[float] | None = None
            if stream_spec:
                timestamps = _collect_packet_timestamps_seconds(src, index, stream_spec)
            if timestamps is not None:
                packets_path = sidecar.with_suffix(".timing.json")
                try:
                    with open(packets_path, "w", encoding="utf-8") as fh:
                        json.dump({"packets": timestamps}, fh, indent=2)
                        fh.write("\n")
                except OSError as write_exc:
                    logging.warning(
                        "failed to write packet timestamps for stream %s: %s",
                        index,
                        write_exc,
                    )
                else:
                    export_entry["packet_timestamps_path"] = str(packets_path)
        exports.append(export_entry)

    return {
        "exports": exports,
        "attachments": [],
        "metadata_path": meta_path,
        "source_metadata": metadata,
        "source_container": source_container,
        "container_artifacts": container_artifacts,
        "container_inventory": container_inventory,
        "dropped_items": dropped_items,
        "container_tags": container_tags,
        "stream_infos": stream_infos,
    }


def _print_command(cmd: Sequence[str]) -> None:
    if not VERBOSE_LEVEL:
        return
    cmdline = " ".join(shlex.quote(str(part)) for part in cmd)
    print(cmdline, file=sys.stderr)


def _packet_sidecar_path(export: StreamExport, export_path: pathlib.Path) -> pathlib.Path | None:
    packet_path_str = export.get("packet_timestamps_path")
    if packet_path_str:
        return pathlib.Path(packet_path_str)
    if export.get("stype") == "d" and not export.get("mkv_ok"):
        timing_path = export_path.with_suffix(".timing.json")
        if timing_path.exists():
            return timing_path
    return None


def _write_json_artifact(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _dropped_stream_indices(dropped_items: Sequence[dict[str, Any]]) -> set[int]:
    indices: set[int] = set()
    for item in dropped_items:
        raw_index = item.get("stream_index")
        if isinstance(raw_index, int):
            indices.add(raw_index)
    return indices


def _exported_stream_indices(exports: Sequence[StreamExport]) -> set[int]:
    indices: set[int] = set()
    for export in exports:
        stream = export.get("stream", {})
        try:
            indices.add(int(cast(Any, stream.get("index"))))
        except (TypeError, ValueError):
            continue
    return indices


def _unaccounted_stream_errors(
    stream_infos: Sequence[StreamInfo],
    exports: Sequence[StreamExport],
    dropped_items: Sequence[dict[str, Any]],
    *,
    data_copy_specs: set[str] | None = None,
) -> list[str]:
    exported = _exported_stream_indices(exports)
    dropped = _dropped_stream_indices(dropped_items)
    data_copy_specs = data_copy_specs or set()
    errors: list[str] = []
    for info in stream_infos:
        index = info["index"]
        stype = info["stype"]
        spec = info.get("spec") or ""
        if index in dropped or index in exported:
            continue
        if stype in {"v", "a", "t"} and spec:
            continue
        if stype == "s" and info.get("mkv_ok") and spec:
            continue
        if stype == "d" and spec in data_copy_specs:
            continue
        stream = info.get("stream", {})
        errors.append(
            "unaccounted source stream "
            f"{index} ({stream.get('codec_type') or stype}/"
            f"{stream.get('codec_name') or stream.get('codec_tag_string') or 'unknown'}); "
            f"use --drop-source-artifact stream:{index} to intentionally drop it"
        )
    return errors


def _stream_transform_records(
    stream_infos: Sequence[StreamInfo],
    exports: Sequence[StreamExport],
    dropped_items: Sequence[dict[str, Any]],
    *,
    video_output_indices: dict[str, int],
    audio_output_indices: dict[str, int],
    subtitle_output_indices: dict[str, int],
    attachment_output_indices: dict[str, int],
    data_output_indices: dict[str, int],
    src_video_copy: set[str],
    src_audio_copy: set[str],
    selected_output_path: pathlib.Path,
    encode_output_path: pathlib.Path,
    use_constant_quality: bool,
    constant_quality: int | None,
    global_video_kbps: int,
    audio_kbps: int,
    svt_lp: int,
    video_codec: str = "libsvtav1",
    video_encoder: dict[str, Any] | None = None,
    audio_codec: str = "libopus",
    audio_encoder: dict[str, Any] | None = None,
    stream_artifact_arcnames: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    exported = {
        index: export
        for export in exports
        if (index := _stream_export_source_index(export)) is not None
    }
    dropped = _dropped_stream_indices(dropped_items)
    selected_original = selected_output_path != encode_output_path
    artifact_arcnames = stream_artifact_arcnames or {}
    records: list[dict[str, Any]] = []
    for info in sorted(stream_infos, key=lambda item: item["index"]):
        index = info["index"]
        stream = info["stream"]
        stype = info["stype"]
        spec = info.get("spec") or ""
        record: dict[str, Any] = {
            "source_stream_index": index,
            "source_stream_spec": spec,
            "source_stream_type": stype,
            "source_stream": stream,
        }
        if index in dropped:
            record["action"] = "intentionally_dropped"
        elif index in exported:
            export = exported[index]
            export_path = pathlib.Path(export["path"])
            artifact_arcname = artifact_arcnames.get(index) or f"streams/{export_path.name}"
            record.update(
                {
                    "action": "preserved_as_source_artifact",
                    "artifact": artifact_arcname,
                    "muxer": export.get("muxer"),
                }
            )
        elif stype == "v":
            if selected_original or spec in src_video_copy:
                record["action"] = "copied"
                record["codec"] = "copy"
            else:
                record["action"] = "re-encoded"
                record["codec"] = video_codec
                if use_constant_quality:
                    record["constant_quality"] = constant_quality
                else:
                    record["target_video_kbps"] = global_video_kbps
                if video_codec == "libsvtav1":
                    record["preset"] = 5
                    record["svtav1_params"] = {"lp": svt_lp}
                if video_encoder:
                    record["encoder_settings"] = dict(video_encoder)
            if spec in video_output_indices:
                record["archive_output_stream_index"] = video_output_indices[spec]
                record["archive_output_stream_type"] = "video"
        elif stype == "a":
            if selected_original or spec in src_audio_copy:
                record["action"] = "copied"
                record["codec"] = "copy"
            else:
                record["action"] = "re-encoded"
                record["codec"] = audio_codec
                record["audio_bitrate_kbps"] = audio_kbps
                record["sample_rate"] = 48000
                if audio_encoder:
                    record["encoder_settings"] = dict(audio_encoder)
            if spec in audio_output_indices:
                record["archive_output_stream_index"] = audio_output_indices[spec]
                record["archive_output_stream_type"] = "audio"
        elif stype in {"s", "t"} and spec:
            record["action"] = "copied"
            record["codec"] = "copy"
            if stype == "s" and spec in subtitle_output_indices:
                record["archive_output_stream_index"] = subtitle_output_indices[spec]
                record["archive_output_stream_type"] = "subtitle"
            elif stype == "t" and spec in attachment_output_indices:
                record["archive_output_stream_index"] = attachment_output_indices[spec]
                record["archive_output_stream_type"] = "attachment"
        elif stype == "d" and spec in data_output_indices:
            record["action"] = "copied"
            record["codec"] = "copy"
            record["archive_output_stream_index"] = data_output_indices[spec]
            record["archive_output_stream_type"] = "data"
        else:
            record["action"] = "unaccounted"
        records.append(record)
    return records


def _source_inventory_payload(
    *,
    src: str,
    output: str,
    metadata: dict[str, Any],
    source_container: dict[str, Any],
    container_inventory: Sequence[dict[str, Any]],
    stream_transforms: Sequence[dict[str, Any]],
    dropped_items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    chapters = metadata.get("chapters") if isinstance(metadata, dict) else []
    programs = metadata.get("programs") if isinstance(metadata, dict) else []
    return {
        "schema_version": 1,
        "kind": "munchy.source-inventory",
        "source": os.path.basename(src),
        "output": output,
        "created_at": now_utc_iso(),
        "strict_accounting": True,
        "container": {
            "format": metadata.get("format") if isinstance(metadata, dict) else None,
            "rebuild": source_container,
            "top_level_atoms": list(container_inventory),
        },
        "streams": list(stream_transforms),
        "chapters": {
            "action": "preserved_as_structured_metadata",
            "items": chapters if isinstance(chapters, list) else [],
        },
        "programs": {
            "action": "preserved_as_structured_metadata",
            "items": programs if isinstance(programs, list) else [],
        },
        "dropped": list(dropped_items),
    }


def _stream_transforms_payload(
    *,
    src: str,
    output: str,
    encode_cmd: Sequence[str],
    selected_output_path: pathlib.Path,
    encode_output_path: pathlib.Path,
    stream_transforms: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    selected_method = "original_remux" if selected_output_path != encode_output_path else "encoded"
    return {
        "schema_version": 1,
        "kind": "munchy.stream-transforms",
        "source": os.path.basename(src),
        "output": output,
        "created_at": now_utc_iso(),
        "selected_output_method": selected_method,
        "encode_command": list(encode_cmd),
        "streams": list(stream_transforms),
    }


def _rebuild_plan_payload(
    *,
    src: str,
    output: str,
    stream_transforms: Sequence[dict[str, Any]],
    source_container: dict[str, Any],
    container_inventory: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "munchy.rebuild-plan",
        "source": os.path.basename(src),
        "archive_output": output,
        "created_at": now_utc_iso(),
        "contract": (
            "Rebuild an original-style container using archive media streams "
            "plus preserved source artifacts; byte-identical output is not "
            "promised after re-encoding."
        ),
        "media_source": "archive_mkv",
        "source_container": source_container,
        "stream_transforms": list(stream_transforms),
        "container_atoms": list(container_inventory),
        "requires_atom_reinsertion": any(
            item.get("action") == "preserved_as_source_artifact"
            and item.get("kind") == "top_level_atom"
            for item in container_inventory
        ),
    }


def _guess_mime_type(path: pathlib.Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    return "application/octet-stream"


def _stream_export_source_index(export: Mapping[str, Any]) -> int | None:
    stream = export.get("stream")
    if not isinstance(stream, Mapping):
        return None
    try:
        return int(cast(Any, stream.get("index")))
    except (TypeError, ValueError):
        return None


def _assign_source_stream_artifact_arcnames(
    container_artifacts: Sequence[SourceArtifact],
    exports: Sequence[StreamExport],
) -> dict[int, str]:
    used_names = {"manifest.json"}
    for artifact in container_artifacts:
        used_names.add(artifact.arcname)

    by_stream_index: dict[int, str] = {}
    for export in exports:
        export_dict = cast(dict[str, Any], export)
        existing = export_dict.get("artifact_arcname")
        if isinstance(existing, str) and existing:
            artifact_name = existing
            used_names.add(artifact_name)
        else:
            export_path = pathlib.Path(str(export["path"]))
            artifact_name = _artifact_arcname(f"streams/{export_path.name}", used_names)
            export_dict["artifact_arcname"] = artifact_name

        source_index = _stream_export_source_index(export)
        if source_index is not None:
            by_stream_index[source_index] = artifact_name

    return by_stream_index


def _source_stream_artifact_records(
    exports: Sequence[StreamExport],
    used_names: set[str],
) -> list[SourceArtifact]:
    artifacts: list[SourceArtifact] = []
    for export in exports:
        export_path = pathlib.Path(str(export["path"]))
        stream = export.get("stream", {})
        codec_hint = cast(
            str,
            stream.get("codec_name") or stream.get("codec_tag_string") or "unknown",
        )
        export_dict = cast(dict[str, Any], export)
        artifact_name = export_dict.get("artifact_arcname")
        if not isinstance(artifact_name, str) or not artifact_name:
            artifact_name = _artifact_arcname(f"streams/{export_path.name}", used_names)
            export_dict["artifact_arcname"] = artifact_name
        else:
            used_names.add(artifact_name)

        artifacts.append(
            SourceArtifact(
                export_path,
                artifact_name,
                "stream_export",
                f"{str(export['stype']).upper()} stream export ({codec_hint})",
                _guess_mime_type(export_path),
                {
                    "stream_type": export.get("stype"),
                    "stream_spec": export.get("spec"),
                    "muxer": export.get("muxer"),
                    "codec": codec_hint,
                    "source_stream": stream,
                },
            )
        )

        packet_sidecar = _packet_sidecar_path(export, export_path)
        if packet_sidecar is not None and packet_sidecar.exists():
            packet_name = _artifact_arcname(
                f"streams/{packet_sidecar.name}",
                used_names,
            )
            artifacts.append(
                SourceArtifact(
                    packet_sidecar,
                    packet_name,
                    "packet_timestamps",
                    f"{str(export['stype']).upper()} stream packet timestamps",
                    "application/json",
                    {
                        "stream_type": export.get("stype"),
                        "stream_spec": export.get("spec"),
                        "for_artifact": artifact_name,
                    },
                )
            )
    return artifacts


def _assemble_source_artifact_bundle_inputs(
    *,
    work_dir: pathlib.Path,
    src: str,
    output: str,
    source_metadata: dict[str, Any],
    source_container: dict[str, Any],
    container_inventory: Sequence[dict[str, Any]],
    container_artifacts: Sequence[SourceArtifact],
    exports: Sequence[StreamExport],
    stream_transforms: Sequence[dict[str, Any]],
    dropped_items: Sequence[dict[str, Any]],
    encode_cmd: Sequence[str],
    selected_output_path: pathlib.Path,
    encode_output_path: pathlib.Path,
    source_filesystem_metadata: Mapping[str, Any] | None = None,
) -> list[SourceArtifact]:
    inventory_root = work_dir / "inventory"
    encoding_root = work_dir / "encoding"
    rebuild_root = work_dir / "rebuild"
    source_ffprobe_path = inventory_root / "source-ffprobe.json"
    source_filesystem_path = inventory_root / "source-filesystem.json"
    source_inventory_path = inventory_root / "source-inventory.json"
    stream_transforms_path = encoding_root / "stream-transforms.json"
    rebuild_plan_path = rebuild_root / "rebuild-plan.json"
    if not isinstance(source_filesystem_metadata, Mapping):
        raise RuntimeError(
            "unresumable: source filesystem metadata sidecar is missing for "
            f"{os.path.basename(src)}"
        )
    filesystem_metadata = dict(source_filesystem_metadata)

    _write_json_artifact(source_ffprobe_path, source_metadata)
    _write_json_artifact(source_filesystem_path, filesystem_metadata)
    _write_json_artifact(
        source_inventory_path,
        _source_inventory_payload(
            src=src,
            output=output,
            metadata=source_metadata,
            source_container=source_container,
            container_inventory=container_inventory,
            stream_transforms=stream_transforms,
            dropped_items=dropped_items,
        ),
    )
    _write_json_artifact(
        stream_transforms_path,
        _stream_transforms_payload(
            src=src,
            output=output,
            encode_cmd=encode_cmd,
            selected_output_path=selected_output_path,
            encode_output_path=encode_output_path,
            stream_transforms=stream_transforms,
        ),
    )
    _write_json_artifact(
        rebuild_plan_path,
        _rebuild_plan_payload(
            src=src,
            output=output,
            stream_transforms=stream_transforms,
            source_container=source_container,
            container_inventory=container_inventory,
        ),
    )

    artifacts: list[SourceArtifact] = []
    used_names = {"manifest.json"}
    for artifact in container_artifacts:
        artifacts.append(artifact)
        used_names.add(artifact.arcname)
    artifacts.extend(_source_stream_artifact_records(exports, used_names))

    for artifact_path, arcname, kind, description, mime_type in (
        (
            source_ffprobe_path,
            "inventory/source-ffprobe.json",
            "source_ffprobe",
            "Raw ffprobe metadata for original source",
            "application/json",
        ),
        (
            source_filesystem_path,
            "inventory/source-filesystem.json",
            "source_filesystem",
            "Original source filesystem metadata",
            "application/json",
        ),
        (
            source_inventory_path,
            "inventory/source-inventory.json",
            "source_inventory",
            "Strict source component inventory",
            "application/json",
        ),
        (
            stream_transforms_path,
            "encoding/stream-transforms.json",
            "stream_transforms",
            "Archive stream transform plan",
            "application/json",
        ),
        (
            rebuild_plan_path,
            "rebuild/rebuild-plan.json",
            "rebuild_plan",
            "Source container rebuild plan",
            "application/json",
        ),
    ):
        used_names.add(arcname)
        artifacts.append(
            SourceArtifact(
                artifact_path,
                arcname,
                kind,
                description,
                mime_type,
                {},
            )
        )

    return artifacts


def _source_artifacts_path(video_path: str) -> str:
    return f"{video_path}{SOURCE_ARTIFACTS_SUFFIX}"


def _sha256_path(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_arcname(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    path = pathlib.PurePosixPath(base)
    stem = path.stem
    suffix = "".join(path.suffixes)
    parent = path.parent
    for index in range(2, 10_000):
        candidate_name = f"{stem}-{index}{suffix}"
        candidate = (
            candidate_name if str(parent) == "." else f"{parent.as_posix()}/{candidate_name}"
        )
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError(f"too many duplicate source artifact names for {base}")


def _source_artifacts_use_zstd(bundle_path: pathlib.Path) -> bool:
    return any(suffix.lower() == ".zst" for suffix in bundle_path.suffixes)


def _zstd_command() -> str:
    command = os.environ.get("VCRUNCH_ZSTD", "zstd")
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(
            "zstd is required for Munchy source artifact bundles; install the zstd package"
        )
    return resolved


def _write_source_artifacts_tar(
    tar: tarfile.TarFile,
    manifest_bytes: bytes,
    artifacts: Sequence[SourceArtifact],
) -> None:
    info = tarfile.TarInfo("manifest.json")
    info.size = len(manifest_bytes)
    info.mtime = int(datetime.now(UTC).timestamp())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(manifest_bytes))
    for artifact in artifacts:
        logging.info(
            "including source artifact %s: %s",
            artifact.arcname,
            _format_size_for_log(artifact.path.stat().st_size),
        )
        tar.add(artifact.path, arcname=artifact.arcname, recursive=False)


def _finish_zstd_process(proc: subprocess.Popen[bytes], label: str) -> None:
    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    if proc.stdout is not None and not proc.stdout.closed:
        proc.stdout.close()
    if proc.stderr is not None:
        stderr = proc.stderr.read()
        proc.stderr.close()
    else:
        stderr = b""
    returncode = proc.wait()
    if returncode == 0:
        return
    detail = stderr.decode("utf-8", "replace").strip() or f"exit code {returncode}"
    raise RuntimeError(f"{label} failed: {detail}")


def _build_source_artifacts_bundle(
    bundle_path: pathlib.Path,
    artifacts: Sequence[SourceArtifact],
    *,
    src: str,
    output: str,
    dropped_items: Sequence[dict[str, Any]] = (),
) -> bool:
    existing = [artifact for artifact in artifacts if artifact.path.exists()]
    if not existing:
        return False

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, Any]] = []
    for artifact in existing:
        try:
            size = artifact.path.stat().st_size
        except OSError as exc:
            raise RuntimeError(
                f"source artifact size unavailable for {artifact.path}: {exc}"
            ) from exc
        manifest_entries.append(
            {
                "path": artifact.arcname,
                "kind": artifact.kind,
                "description": artifact.description,
                "mime_type": artifact.mime_type,
                "bytes": size,
                "sha256": _sha256_path(artifact.path),
                **artifact.metadata,
            }
        )

    manifest = {
        "schema_version": 1,
        "kind": "munchy.source-artifacts",
        "source": os.path.basename(src),
        "output": output,
        "created_at": now_utc_iso(),
        "artifacts": manifest_entries,
        "dropped": list(dropped_items),
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    try:
        if _source_artifacts_use_zstd(bundle_path):
            proc = subprocess.Popen(
                [
                    _zstd_command(),
                    f"-{SOURCE_ARTIFACTS_ZSTD_LEVEL}",
                    f"--long={SOURCE_ARTIFACTS_ZSTD_LONG}",
                    "-T0",
                    "-q",
                    "-f",
                    "-o",
                    str(bundle_path),
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if proc.stdin is None:
                raise RuntimeError("failed to open zstd stdin")
            try:
                with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                    _write_source_artifacts_tar(tar, manifest_bytes, existing)
                _finish_zstd_process(proc, "zstd source artifact compression")
            except Exception:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                raise
        else:
            with tarfile.open(bundle_path, "w") as tar:
                _write_source_artifacts_tar(tar, manifest_bytes, existing)
    except (OSError, tarfile.TarError, subprocess.SubprocessError, RuntimeError) as exc:
        bundle_path.unlink(missing_ok=True)
        raise RuntimeError(f"failed to build source artifact bundle: {exc}") from exc
    return True


def _extract_source_artifacts_tar(
    tar: tarfile.TarFile,
    dest_dir: pathlib.Path,
) -> None:
    dest_root = dest_dir.resolve()
    for member in tar:
        name = pathlib.PurePosixPath(member.name)
        if not member.name or name.is_absolute() or ".." in name.parts:
            raise RuntimeError(f"unsafe source artifact path in tar: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise RuntimeError(f"unsupported source artifact tar entry: {member.name!r}")
        target = (dest_dir / member.name).resolve()
        if target != dest_root and dest_root not in target.parents:
            raise RuntimeError(f"unsafe source artifact path in tar: {member.name!r}")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        source = tar.extractfile(member)
        if source is None:
            raise RuntimeError(f"failed to read source artifact tar entry: {member.name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, target.open("wb") as out:
            shutil.copyfileobj(source, out)
        try:
            target.chmod(member.mode & 0o777)
        except OSError:
            pass


def _safe_extract_source_artifacts(
    bundle_path: pathlib.Path,
    dest_dir: pathlib.Path,
) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        if _source_artifacts_use_zstd(bundle_path):
            proc = subprocess.Popen(
                [_zstd_command(), "-d", "-c", "-q", str(bundle_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if proc.stdout is None:
                raise RuntimeError("failed to open zstd stdout")
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                    _extract_source_artifacts_tar(tar, dest_dir)
                _finish_zstd_process(proc, "zstd source artifact decompression")
            except Exception:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                raise
        else:
            with tarfile.open(bundle_path, "r") as tar:
                _extract_source_artifacts_tar(tar, dest_dir)
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"failed to extract source artifact bundle: {exc}") from exc


def _read_json_artifact(root: pathlib.Path, arcname: str) -> dict[str, Any]:
    path = root / arcname
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing source artifact JSON: {arcname}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid source artifact JSON {arcname}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"source artifact JSON is not an object: {arcname}")
    return cast(dict[str, Any], payload)


def _artifact_entries_by_path(
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    entries = manifest.get("artifacts")
    if not isinstance(entries, list):
        return {}
    by_path: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str):
            by_path[path] = cast(dict[str, Any], entry)
    return by_path


def _artifact_path_for_kind(
    manifest: dict[str, Any],
    kind: str,
    default: str,
) -> str:
    for entry in _artifact_entries_by_path(manifest).values():
        if entry.get("kind") == kind and isinstance(entry.get("path"), str):
            return cast(str, entry["path"])
    return default


def _top_level_atom_entry(
    manifest: dict[str, Any],
    atom_type: str,
) -> dict[str, Any] | None:
    for entry in _artifact_entries_by_path(manifest).values():
        if (
            entry.get("kind") == "top_level_container_atom"
            and isinstance(entry.get("atom_type"), str)
            and cast(str, entry["atom_type"]).lower() == atom_type.lower()
        ):
            return entry
    return None


def _source_artifact_streams(root: pathlib.Path) -> list[dict[str, Any]]:
    manifest = _read_json_artifact(root, "manifest.json")
    transforms_path = _artifact_path_for_kind(
        manifest,
        "stream_transforms",
        "encoding/stream-transforms.json",
    )
    try:
        transforms = _read_json_artifact(root, transforms_path)
    except RuntimeError:
        inventory_path = _artifact_path_for_kind(
            manifest,
            "source_inventory",
            "inventory/source-inventory.json",
        )
        transforms = _read_json_artifact(root, inventory_path)
    streams = transforms.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError("source artifacts contain no stream transform records")
    return [cast(dict[str, Any], item) for item in streams if isinstance(item, dict)]


def _run_checked_command(cmd: Sequence[str], label: str) -> None:
    _print_command(cmd)
    proc = subprocess.run(
        list(cmd),
        capture_output=True,
    )
    if proc.returncode == 0:
        return
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    stdout = proc.stdout.decode("utf-8", "replace").strip()
    detail = stderr or stdout or f"exit code {proc.returncode}"
    raise RuntimeError(f"{label} failed: {detail}")


def _extract_raw_stream_payload(
    media_path: pathlib.Path,
    stream_spec: str,
    output_path: pathlib.Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run_checked_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(media_path),
            "-map",
            stream_spec,
            "-c",
            "copy",
            "-f",
            "data",
            str(output_path),
        ],
        f"extract raw stream payload from {media_path}",
    )


def _audit_preserved_stream_payload(
    *,
    original_source: pathlib.Path,
    artifact_path: pathlib.Path,
    stream: dict[str, Any],
    work_dir: pathlib.Path,
) -> tuple[bool, str]:
    raw_index = stream.get("source_stream_index")
    if not isinstance(raw_index, int):
        raw_index = stream.get("source_stream", {}).get("index")
    try:
        source_stream_index = int(raw_index)
    except (TypeError, ValueError):
        return False, "preserved stream has no numeric source stream index"

    source_payload = work_dir / f"source-stream-{source_stream_index}.bin"
    artifact_payload = work_dir / f"artifact-stream-{source_stream_index}.bin"
    _extract_raw_stream_payload(
        original_source,
        f"0:{source_stream_index}",
        source_payload,
    )
    if artifact_path.suffix.lower() in _ISO_BMFF_CONTAINER_SUFFIXES:
        _extract_raw_stream_payload(artifact_path, "0:0", artifact_payload)
        artifact_compare_path = artifact_payload
    else:
        artifact_compare_path = artifact_path
    source_sha = _sha256_path(source_payload)
    artifact_sha = _sha256_path(artifact_compare_path)
    if source_sha != artifact_sha:
        return (
            False,
            "preserved stream payload differs: "
            f"source sha256={source_sha}, artifact sha256={artifact_sha}",
        )
    return True, f"stream {source_stream_index} payload sha256={source_sha}"


def _audit_extracted_source_artifacts(
    root: pathlib.Path,
    *,
    archive_mkv: pathlib.Path | None = None,
    original_source: pathlib.Path | None = None,
    deep: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []
    rebuild_blockers: list[str] = []
    rebuild_supported = True

    try:
        manifest = _read_json_artifact(root, "manifest.json")
    except RuntimeError as exc:
        return {
            "ok": False,
            "errors": [str(exc)],
            "warnings": warnings,
            "checks": checks,
            "rebuild_supported": False,
            "rebuild_blockers": ["manifest"],
        }

    if manifest.get("kind") != "munchy.source-artifacts":
        errors.append("manifest kind is not munchy.source-artifacts")
    if manifest.get("schema_version") != 1:
        errors.append("unsupported source artifact manifest schema version")

    artifact_entries = _artifact_entries_by_path(manifest)
    if not artifact_entries:
        errors.append("manifest contains no artifact entries")

    hashes_checked = 0
    bytes_checked = 0
    for arcname, entry in sorted(artifact_entries.items()):
        path = root / arcname
        if not path.exists():
            errors.append(f"missing artifact file: {arcname}")
            continue
        expected_size = entry.get("bytes")
        if isinstance(expected_size, int):
            actual_size = path.stat().st_size
            if actual_size != expected_size:
                errors.append(
                    f"artifact size mismatch for {arcname}: "
                    f"expected {expected_size}, got {actual_size}"
                )
            else:
                bytes_checked += 1
        expected_sha = entry.get("sha256")
        if isinstance(expected_sha, str):
            actual_sha = _sha256_path(path)
            if actual_sha != expected_sha:
                errors.append(
                    f"artifact sha256 mismatch for {arcname}: "
                    f"expected {expected_sha}, got {actual_sha}"
                )
            else:
                hashes_checked += 1

    checks.append(f"artifact sizes checked: {bytes_checked}")
    checks.append(f"artifact sha256 hashes checked: {hashes_checked}")

    source_rebuild_mode = ""
    try:
        inventory_path = _artifact_path_for_kind(
            manifest,
            "source_inventory",
            "inventory/source-inventory.json",
        )
        inventory = _read_json_artifact(root, inventory_path)
        if inventory.get("strict_accounting") is not True:
            errors.append("source inventory does not declare strict_accounting=true")
        container = inventory.get("container")
        container_format = container.get("format") if isinstance(container, dict) else None
        container_rebuild = container.get("rebuild") if isinstance(container, dict) else None
        if not isinstance(container_rebuild, dict):
            errors.append("source inventory missing source container rebuild contract")
            rebuild_supported = False
            rebuild_blockers.append("source_container_contract")
        elif container_rebuild.get("supported") is True:
            raw_mode = container_rebuild.get("mode")
            source_rebuild_mode = raw_mode if isinstance(raw_mode, str) else ""
            checks.append(
                f"source container rebuild contract: {container_rebuild.get('mode') or 'supported'}"
            )
        else:
            warnings.append(
                "source container is not rebuild-supported: "
                f"{container_rebuild.get('message') or container_rebuild.get('mode')}"
            )
            rebuild_supported = False
            rebuild_blockers.append("source_container")
        format_tags = container_format.get("tags") if isinstance(container_format, dict) else None
        if isinstance(format_tags, dict):
            metadata_keys = sorted(
                key
                for key in format_tags
                if key
                not in {
                    "major_brand",
                    "minor_version",
                    "compatible_brands",
                    "encoder",
                }
            )
            if metadata_keys:
                if source_rebuild_mode == "iso_bmff_rebuild":
                    checks.append(
                        "source format metadata tags available for moov graft: "
                        f"{len(metadata_keys)}"
                    )
                else:
                    checks.append(f"source format metadata tags recorded: {len(metadata_keys)}")
    except RuntimeError as exc:
        errors.append(str(exc))
        inventory = {}

    if source_rebuild_mode in {"matroska_rebuild", "webm_rebuild"}:
        try:
            matroska_identification_path = _artifact_path_for_kind(
                manifest,
                "matroska_identification",
                "container/matroska-identification.json",
            )
            matroska_identification = _read_json_artifact(
                root,
                matroska_identification_path,
            )
            if not isinstance(matroska_identification.get("tracks"), list):
                errors.append("Matroska source identification has no tracks list")
                rebuild_supported = False
                rebuild_blockers.append("matroska_identification")
            else:
                checks.append("Matroska/WebM source identification checked")
        except RuntimeError as exc:
            errors.append(str(exc))
            rebuild_supported = False
            rebuild_blockers.append("matroska_identification")

    try:
        streams = _source_artifact_streams(root)
    except RuntimeError as exc:
        errors.append(str(exc))
        streams = []

    dropped = manifest.get("dropped")
    if isinstance(dropped, list) and dropped:
        warnings.append(f"source artifacts record intentional drops: {len(dropped)}")

    preserved_streams = 0
    unaccounted_streams = 0
    for stream in streams:
        action = stream.get("action")
        if action == "unaccounted":
            unaccounted_streams += 1
            errors.append(f"unaccounted source stream: {stream.get('source_stream_index')}")
            rebuild_supported = False
            rebuild_blockers.append("stream_accounting")
        elif action == "preserved_as_source_artifact":
            preserved_streams += 1
            artifact = stream.get("artifact")
            if not isinstance(artifact, str):
                errors.append(
                    "preserved source stream has no artifact path: "
                    f"{stream.get('source_stream_index')}"
                )
                rebuild_supported = False
                rebuild_blockers.append("stream_artifact")
                continue
            artifact_path = root / artifact
            if not artifact_path.exists():
                errors.append(f"missing preserved source stream artifact: {artifact}")
                rebuild_supported = False
                rebuild_blockers.append("stream_artifact")
            elif artifact_path.suffix.lower() not in _ISO_BMFF_CONTAINER_SUFFIXES:
                source_index = stream.get("source_stream_index")
                drop_hint = (
                    f"; use --drop-source-artifact stream:{source_index} to intentionally drop it"
                    if isinstance(source_index, int)
                    else ""
                )
                errors.append(
                    "preserved source stream artifact is not currently "
                    f"rebuild-muxable: {artifact}{drop_hint}"
                )
                rebuild_supported = False
                rebuild_blockers.append("stream_artifact")
    checks.append(f"preserved stream artifacts checked: {preserved_streams}")
    if unaccounted_streams:
        checks.append(f"unaccounted streams: {unaccounted_streams}")

    if archive_mkv is not None:
        if not archive_mkv.exists():
            errors.append(f"archive media file is missing: {archive_mkv}")
        else:
            try:
                archive_meta = ffprobe_json(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-of",
                        "json",
                        str(archive_mkv),
                    ]
                )
                archive_streams = archive_meta.get("streams", [])
                if isinstance(archive_streams, list):
                    by_type: dict[str, int] = {}
                    for item in archive_streams:
                        if not isinstance(item, dict):
                            continue
                        codec_type = item.get("codec_type")
                        if isinstance(codec_type, str):
                            by_type[codec_type] = by_type.get(codec_type, 0) + 1
                    for stream in streams:
                        archive_type = stream.get("archive_output_stream_type")
                        archive_index = stream.get("archive_output_stream_index")
                        if not isinstance(archive_type, str) or not isinstance(
                            archive_index,
                            int,
                        ):
                            continue
                        if archive_index >= by_type.get(archive_type, 0):
                            errors.append(
                                f"archive output stream missing: {archive_type}:{archive_index}"
                            )
                checks.append("archive media streams probed")
            except Exception as exc:
                errors.append(f"failed to probe archive media: {exc}")

    if deep and original_source is not None:
        if not original_source.exists():
            errors.append(f"original source is missing: {original_source}")
        else:
            deep_dir = root / ".audit-payloads"
            deep_dir.mkdir(parents=True, exist_ok=True)
            for stream in streams:
                if stream.get("action") != "preserved_as_source_artifact":
                    continue
                artifact = stream.get("artifact")
                if not isinstance(artifact, str):
                    continue
                try:
                    ok, message = _audit_preserved_stream_payload(
                        original_source=original_source,
                        artifact_path=root / artifact,
                        stream=stream,
                        work_dir=deep_dir,
                    )
                except Exception as exc:
                    ok = False
                    message = f"preserved stream payload audit failed: {exc}"
                if ok:
                    checks.append(message)
                else:
                    errors.append(message)

    if source_rebuild_mode in {"matroska_rebuild", "webm_rebuild"}:
        rebuild_scope = (
            "archive Matroska media streams plus copied subtitles/attachments "
            "plus Matroska/WebM source identification"
        )
    else:
        rebuild_scope = (
            "archive media streams plus rebuild-muxable preserved stream "
            "artifacts plus container-level moov metadata plus selected "
            "top-level atoms"
        )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "source": manifest.get("source"),
        "output": manifest.get("output"),
        "rebuild_supported": rebuild_supported and not errors,
        "rebuild_blockers": sorted(set(rebuild_blockers)),
        "rebuild_scope": rebuild_scope,
        "artifacts_checked": len(artifact_entries),
    }


def _audit_source_artifacts_bundle(
    bundle_path: pathlib.Path,
    *,
    archive_mkv: pathlib.Path | None = None,
    original_source: pathlib.Path | None = None,
    deep: bool = False,
    work_dir: pathlib.Path | None = None,
    keep_work: bool = False,
) -> dict[str, Any]:
    base_dir: pathlib.Path
    cleanup = False
    if work_dir is None:
        base_dir = pathlib.Path(tempfile.mkdtemp(prefix="munchy-audit-"))
        cleanup = not keep_work
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        base_dir = pathlib.Path(tempfile.mkdtemp(prefix="audit-", dir=str(work_dir)))
        cleanup = not keep_work
    try:
        root = base_dir / "artifacts"
        _safe_extract_source_artifacts(bundle_path, root)
        result = _audit_extracted_source_artifacts(
            root,
            archive_mkv=archive_mkv,
            original_source=original_source,
            deep=deep,
        )
        if keep_work:
            result["work_dir"] = str(base_dir)
        return result
    finally:
        if cleanup:
            shutil.rmtree(base_dir, ignore_errors=True)


def _archive_map_args_for_rebuild(
    streams: Sequence[dict[str, Any]],
) -> list[str]:
    type_spec = {
        "video": "v",
        "audio": "a",
        "subtitle": "s",
    }
    records: list[tuple[int, str, int]] = []
    for stream in streams:
        archive_type = stream.get("archive_output_stream_type")
        archive_index = stream.get("archive_output_stream_index")
        source_index = stream.get("source_stream_index")
        if not isinstance(archive_type, str) or archive_type not in type_spec:
            continue
        if not isinstance(archive_index, int):
            continue
        order_index = source_index if isinstance(source_index, int) else archive_index
        records.append((order_index, type_spec[archive_type], archive_index))
    if not records:
        return ["-map", "0:v?", "-map", "0:a?"]

    args: list[str] = []
    seen: set[tuple[str, int]] = set()
    for _source_index, stream_type, archive_index in sorted(records):
        key = (stream_type, archive_index)
        if key in seen:
            continue
        seen.add(key)
        args += ["-map", f"0:{stream_type}:{archive_index}"]
    return args


def _source_container_mode(
    artifacts_root: pathlib.Path,
    manifest: dict[str, Any],
) -> str:
    inventory_path = _artifact_path_for_kind(
        manifest,
        "source_inventory",
        "inventory/source-inventory.json",
    )
    inventory = _read_json_artifact(artifacts_root, inventory_path)
    container = inventory.get("container")
    if not isinstance(container, dict):
        return ""
    rebuild = container.get("rebuild")
    if not isinstance(rebuild, dict):
        return ""
    mode = rebuild.get("mode")
    return mode if isinstance(mode, str) else ""


def _rebuild_matroska_source_container(
    archive_mkv: pathlib.Path,
    output_path: pathlib.Path,
    *,
    source_mode: str,
) -> dict[str, Any]:
    mkvmerge = shutil.which("mkvmerge")
    if not mkvmerge:
        raise RuntimeError("mkvmerge is required to rebuild Matroska/WebM containers")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_name(output_path.name + ".part")
    try:
        part_path.unlink()
    except FileNotFoundError:
        pass
    cmd = [
        mkvmerge,
        "-o",
        str(part_path),
        "--disable-track-statistics-tags",
    ]
    if output_path.suffix.lower() == ".webm":
        cmd.insert(1, "--webm")
    cmd.append(str(archive_mkv))
    try:
        _run_checked_command(cmd, "Matroska/WebM source container rebuild")
        part_path.replace(output_path)
    finally:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
    return {
        "output": str(output_path),
        "archive_mkv": str(archive_mkv),
        "source_container_mode": source_mode,
        "rebuild_tool": "mkvmerge",
        "webm_output": output_path.suffix.lower() == ".webm",
    }


def _rebuild_source_container(
    archive_mkv: pathlib.Path,
    source_artifacts_tar: pathlib.Path,
    output_path: pathlib.Path,
    *,
    append_atom_types: Sequence[str] = DEFAULT_REBUILD_APPEND_TOP_LEVEL_ATOMS,
    graft_moov_metadata: bool = DEFAULT_REBUILD_GRAFT_MOOV_METADATA,
    work_dir: pathlib.Path | None = None,
    keep_work: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    if output_path.exists() and not overwrite:
        raise RuntimeError(f"output already exists: {output_path}")
    if not archive_mkv.exists():
        raise RuntimeError(f"archive media file is missing: {archive_mkv}")
    if not source_artifacts_tar.exists():
        raise RuntimeError(f"source artifacts bundle is missing: {source_artifacts_tar}")

    cleanup = False
    if work_dir is None:
        base_dir = pathlib.Path(tempfile.mkdtemp(prefix="munchy-rebuild-"))
        cleanup = not keep_work
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        base_dir = pathlib.Path(tempfile.mkdtemp(prefix="rebuild-", dir=str(work_dir)))
        cleanup = not keep_work

    try:
        artifacts_root = base_dir / "artifacts"
        _safe_extract_source_artifacts(source_artifacts_tar, artifacts_root)
        audit = _audit_extracted_source_artifacts(
            artifacts_root,
            archive_mkv=archive_mkv,
        )
        if audit.get("errors"):
            raise RuntimeError(
                "source artifact audit failed before rebuild: "
                + "; ".join(cast(list[str], audit["errors"]))
            )
        if not audit.get("rebuild_supported"):
            messages = cast(list[str], audit.get("warnings") or [])
            raise RuntimeError(
                "source artifacts do not support source-container rebuild: "
                + ("; ".join(messages) if messages else "unsupported rebuild contract")
            )

        manifest = _read_json_artifact(artifacts_root, "manifest.json")
        source_mode = _source_container_mode(artifacts_root, manifest)
        if source_mode in {"matroska_rebuild", "webm_rebuild"}:
            result = _rebuild_matroska_source_container(
                archive_mkv,
                output_path,
                source_mode=source_mode,
            )
            result["source_artifacts"] = str(source_artifacts_tar)
            if keep_work:
                result["work_dir"] = str(base_dir)
            return result

        streams = _source_artifact_streams(artifacts_root)
        muxer = "mov" if output_path.suffix.lower() == ".mov" else "mp4"
        base_mp4 = base_dir / f"archive-media.{muxer}"
        remux_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(archive_mkv),
            *_archive_map_args_for_rebuild(streams),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-write_tmcd",
            "0",
            "-f",
            muxer,
            str(base_mp4),
        ]
        _run_checked_command(remux_cmd, "archive media remux")

        preserved_stream_paths: list[pathlib.Path] = []
        for stream in streams:
            if stream.get("action") != "preserved_as_source_artifact":
                continue
            artifact = stream.get("artifact")
            if not isinstance(artifact, str):
                raise RuntimeError(
                    "preserved stream is missing artifact path: "
                    f"{stream.get('source_stream_index')}"
                )
            stream_path = artifacts_root / artifact
            if stream_path.suffix.lower() not in _ISO_BMFF_CONTAINER_SUFFIXES:
                raise RuntimeError(
                    f"preserved stream artifact is not currently rebuild-muxable: {artifact}"
                )
            preserved_stream_paths.append(stream_path)

        media_with_streams = base_mp4
        if preserved_stream_paths:
            mp4mux = shutil.which("mp4mux")
            if not mp4mux:
                raise RuntimeError(
                    "mp4mux is required to reinsert preserved source stream artifacts"
                )
            muxed_mp4 = base_dir / "with-source-stream-artifacts.mp4"
            mux_cmd = [mp4mux, "--track", f"mp4:{base_mp4}"]
            for stream_path in preserved_stream_paths:
                mux_cmd += ["--track", f"mp4:{stream_path}"]
            mux_cmd.append(str(muxed_mp4))
            _run_checked_command(mux_cmd, "source stream artifact mux")
            media_with_streams = muxed_mp4

        moov_metadata_graft: dict[str, Any] = {
            "grafted_atoms": [],
            "chunk_offsets_adjusted": 0,
            "moov_size_delta": 0,
        }
        media_with_metadata = media_with_streams
        if graft_moov_metadata:
            moov_entry = _top_level_atom_entry(manifest, "moov")
            moov_arcname = moov_entry.get("path") if moov_entry else None
            if isinstance(moov_arcname, str):
                grafted_mp4 = base_dir / "with-moov-metadata.mp4"
                moov_metadata_graft = _graft_moov_metadata_into_file(
                    media_with_streams,
                    artifacts_root / moov_arcname,
                    grafted_mp4,
                )
                if moov_metadata_graft.get("grafted_atoms"):
                    media_with_metadata = grafted_mp4

        append_set = {atom_type.lower() for atom_type in append_atom_types}
        top_level_atom_entries = [
            entry
            for entry in _artifact_entries_by_path(manifest).values()
            if entry.get("kind") == "top_level_container_atom"
            and isinstance(entry.get("atom_type"), str)
            and cast(str, entry["atom_type"]).lower() in append_set
        ]
        top_level_atom_entries.sort(
            key=lambda entry: (
                int(entry.get("atom_offset", 0)) if isinstance(entry.get("atom_offset"), int) else 0
            )
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        final_tmp = base_dir / f"final{output_path.suffix or '.mp4'}"
        appended_atoms: list[str] = []
        with media_with_metadata.open("rb") as src_fh, final_tmp.open("wb") as out_fh:
            shutil.copyfileobj(src_fh, out_fh)
            for entry in top_level_atom_entries:
                arcname = entry.get("path")
                if not isinstance(arcname, str):
                    continue
                atom_path = artifacts_root / arcname
                with atom_path.open("rb") as atom_fh:
                    shutil.copyfileobj(atom_fh, out_fh)
                appended_atoms.append(cast(str, entry["atom_type"]))

        final_tmp.replace(output_path)
        result = {
            "output": str(output_path),
            "archive_mkv": str(archive_mkv),
            "source_artifacts": str(source_artifacts_tar),
            "preserved_stream_artifacts_muxed": [
                str(path.relative_to(artifacts_root)) for path in preserved_stream_paths
            ],
            "moov_metadata_graft": moov_metadata_graft,
            "top_level_atoms_appended": appended_atoms,
        }
        if keep_work:
            result["work_dir"] = str(base_dir)
        return result
    finally:
        if cleanup:
            shutil.rmtree(base_dir, ignore_errors=True)
