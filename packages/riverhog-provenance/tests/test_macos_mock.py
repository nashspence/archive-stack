from __future__ import annotations

import hashlib
import os
from pathlib import Path

from riverhog_provenance import (
    MacOSFileStateObserver,
    ObservationPolicy,
    ObservationRequest,
    PayloadBindingRequest,
    validate_graph_fragment,
)
from riverhog_provenance.macos import ACLCapture, DarwinFileSystemInfo, MacOSBackend
from riverhog_provenance.model import NativeStat


class FakeMacOSNative:
    def __init__(self) -> None:
        self.values = {
            b"com.apple.ResourceFork": b"fork-data",
            b"com.apple.FinderInfo": b"F" * 32,
            b"com.apple.quarantine": b"0083;mock provenance",
            b"com.apple.metadata:kMDItemWhereFroms": b"mock metadata",
        }

    def file_attributes(self, fd: int):
        stat = os.fstat(fd)
        return {
            "birthtime_ns": stat.st_ctime_ns,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "atime_ns": stat.st_atime_ns,
            "backup_time_ns": stat.st_mtime_ns - 1_000,
            "added_time_ns": stat.st_mtime_ns - 2_000,
            "flags": 0x20,
            "generation": 7,
            "document_id": 42,
            "file_id": stat.st_ino,
            "parent_id": 1,
            "finder_info": b"F" * 32,
            "total_size": stat.st_size + len(self.values[b"com.apple.ResourceFork"]),
            "allocation_size": 8192,
            "io_block_size": 4096,
            "data_length": stat.st_size,
            "data_allocation_size": 4096,
            "resource_fork_length": len(self.values[b"com.apple.ResourceFork"]),
            "resource_fork_allocation_size": 4096,
        }

    def filesystem_info(self, fd: int) -> DarwinFileSystemInfo:
        return DarwinFileSystemInfo(
            fs_type="apfs",
            mount_point=b"/",
            mounted_from=b"/dev/disk3s1",
            fsid=(1, 2),
            flags=0,
            subtype=0,
            io_size=4096,
            block_size=4096,
        )

    def volume_attributes(self, mount_point: bytes):
        return {
            "uuid": "12345678-1234-5678-1234-567812345678",
            "capabilities": [0x300, 0x6400, 0, 0],
            "valid_capabilities": [0x300, 0x6400, 0, 0],
        }

    def list_xattrs(self, fd: int):
        return list(self.values)

    def xattr_size(self, fd: int, name: bytes) -> int:
        return len(self.values[name])

    def get_xattr(self, fd: int, name: bytes, maximum: int):
        value = self.values[name]
        return len(value), value if len(value) <= maximum else None

    def digest_resource_fork(self, fd: int, name: bytes, *, chunk_bytes: int):
        value = self.values[name]
        return len(value), hashlib.sha256(value).hexdigest()

    def get_acl(self, fd: int):
        return ACLCapture(raw=b"darwin-acl-external", text="!#acl 1\n")

    def sysctl_text(self, name: str):
        return {
            "kern.osproductversion": "26.6",
            "kern.osversion": "25G84",
            "hw.model": "Mac16,1",
        }.get(name)


def test_mocked_macos_observation_contract(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "photo.jpg"
    content = b"opaque image bytes; never parsed"
    payload.write_bytes(content)
    result = MacOSFileStateObserver(native=FakeMacOSNative(), enforce_platform=False).observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=urn_factory(),
            payload_binding=PayloadBindingRequest(),
        )
    )
    validate_graph_fragment(result.graph_fragment())
    rows = result.state["filesystem_metadata"]["native_metadata"]
    kinds = {row["kind"] for row in rows}
    assert {
        "resource_fork",
        "finder_info",
        "security_label",
        "acl",
        "file_flag",
        "sparse_map",
        "native_stat_field",
    }.issubset(kinds)
    assert result.environment["operating_system"]["version"] == "26.6"
    assert result.environment["filesystem"]["type"] == "apfs"
    assert result.environment["filesystem"]["case_sensitive"] is True
    assert result.capture["outcome"] == "success"


def test_mocked_large_resource_fork_is_digest_only(tmp_path: Path, urn_factory) -> None:
    native = FakeMacOSNative()
    native.values[b"com.apple.ResourceFork"] = b"R" * 128
    payload = tmp_path / "movie.mov"
    payload.write_bytes(b"payload")
    result = MacOSFileStateObserver(native=native, enforce_platform=False).observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=urn_factory(),
            policy=ObservationPolicy(
                inline_native_value_bytes=16,
                maximum_native_value_bytes=1024,
            ),
        )
    )
    fork = next(
        row
        for row in result.state["filesystem_metadata"]["native_metadata"]
        if row["kind"] == "resource_fork"
    )
    assert fork["capture_status"] == "digest_only"
    assert fork["value"]["byte_length"] == 128
    validate_graph_fragment(result.graph_fragment())


def test_macos_volume_uuid_authority_is_host_independent(urn_factory) -> None:
    stat = NativeStat(
        device=9,
        inode=42,
        mode=0o100644,
        nlink=1,
        uid=501,
        gid=20,
        size=7,
        atime_ns=1,
        mtime_ns=2,
        ctime_ns=3,
    )
    fs_info = DarwinFileSystemInfo(
        fs_type="apfs",
        mount_point=b"/Volumes/Archive",
        mounted_from=b"/dev/disk4s1",
        fsid=(11, 22),
        flags=0,
        subtype=0,
        io_size=4096,
        block_size=4096,
    )
    volume_attrs = {"uuid": "12345678-1234-5678-1234-567812345678"}
    first = MacOSBackend._volume_authority(urn_factory(), stat, fs_info, volume_attrs)
    second = MacOSBackend._volume_authority(urn_factory(), stat, fs_info, volume_attrs)
    assert first == second


def test_macos_local_volume_authority_remains_host_scoped(urn_factory) -> None:
    stat = NativeStat(
        device=9,
        inode=42,
        mode=0o100644,
        nlink=1,
        uid=501,
        gid=20,
        size=7,
        atime_ns=1,
        mtime_ns=2,
        ctime_ns=3,
    )
    fs_info = DarwinFileSystemInfo(
        fs_type="apfs",
        mount_point=b"/Volumes/Archive",
        mounted_from=b"/dev/disk4s1",
        fsid=(11, 22),
        flags=0,
        subtype=0,
        io_size=4096,
        block_size=4096,
    )
    first = MacOSBackend._volume_authority(urn_factory(), stat, fs_info, {})
    second = MacOSBackend._volume_authority(urn_factory(), stat, fs_info, {})
    assert first != second


class _VolumeAttributesFailureMacOSNative(FakeMacOSNative):
    def volume_attributes(self, mount_point: bytes):
        raise OSError(5, "mock I/O failure")


def test_macos_volume_attribute_failure_retains_fstatfs_context(
    tmp_path: Path, urn_factory
) -> None:
    payload = tmp_path / "volume-context.dat"
    payload.write_bytes(b"payload")
    result = MacOSFileStateObserver(
        native=_VolumeAttributesFailureMacOSNative(), enforce_platform=False
    ).observe(ObservationRequest(path=payload, lineage_id=urn_factory(), host_id=urn_factory()))
    assert result.environment["filesystem"]["type"] == "apfs"
    assert result.capture["coverage"]["native_identifiers"] == "partial"
    assert result.capture["outcome"] == "partial"
    assert any(
        item["code"] == "volume_attributes_unavailable" for item in result.capture["diagnostics"]
    )
    validate_graph_fragment(result.graph_fragment())
