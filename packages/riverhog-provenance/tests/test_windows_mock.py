from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import stat as statmod
import struct
from pathlib import Path

import pytest
from riverhog_provenance import (
    ObservationPolicy,
    ObservationRequest,
    PayloadBindingRequest,
    SymlinkRefusedError,
    WindowsFileStateObserver,
    validate_graph_fragment,
)
from riverhog_provenance.windows import (
    _BY_HANDLE_FILE_INFORMATION,
    _FILE_ALLOCATED_RANGE_BUFFER,
    _FILE_ATTRIBUTE_TAG_INFO,
    _FILE_BASIC_INFO,
    _FILE_COMPRESSION_INFO,
    _FILE_ID_INFO,
    _FILE_STANDARD_INFO,
    _FILE_STORAGE_INFO,
    _TOKEN_ELEVATION,
    BACKUP_ALTERNATE_DATA,
    BACKUP_EA_DATA,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILETIME_UNIX_EPOCH_TICKS,
    BackupStreamCapture,
    IntegrityInfo,
    OpenedWindowsFile,
    SecurityDescriptorCapture,
    WindowsBackend,
    WindowsIdentity,
    WindowsNativeAPI,
    WindowsPathInfo,
    WindowsPrincipal,
    WindowsSnapshot,
    WindowsVolumeInfo,
    windows_locator,
)


def _ticks(epoch_ns: int) -> int:
    return FILETIME_UNIX_EPOCH_TICKS + epoch_ns // 100


def _ea_entry(name: bytes, value: bytes, *, flags: int = 0, final: bool) -> bytes:
    body = struct.pack("<IBBH", 0, flags, len(name), len(value)) + name + b"\0" + value
    if final:
        return body
    padded = body + b"\0" * ((-len(body)) % 4)
    return struct.pack("<I", len(padded)) + padded[4:]


class FakeWindowsNative:
    def __init__(self) -> None:
        self.snapshot_capture_usn: list[bool] = []
        self.reparse_tag = 0
        self.reparse_data: bytes | None = None

    @staticmethod
    def _identity(path: str | None = None, fd: int | None = None) -> WindowsIdentity:
        st = os.stat(path) if path is not None else os.fstat(fd)  # type: ignore[arg-type]
        value = int(st.st_ino).to_bytes(16, "big", signed=False)
        return WindowsIdentity(volume_serial_number=0xA1B2C3D4, file_id=value)

    def inspect_path(self, path: str, *, follow_non_name_surrogate: bool = True):
        st = os.stat(path)
        return WindowsPathInfo(
            identity=self._identity(path=path),
            attributes=(FILE_ATTRIBUTE_REPARSE_POINT if self.reparse_tag else 0x20),
            reparse_tag=self.reparse_tag,
            directory=statmod.S_ISDIR(st.st_mode),
            reparse_data=self.reparse_data,
        )

    def open_regular_file(self, path: str, request: ObservationRequest):
        fd = os.open(path, os.O_RDONLY)
        info = self.inspect_path(path)
        return (
            OpenedWindowsFile(
                fd=fd,
                identity=info.identity,
                attributes=info.attributes,
                reparse_tag=info.reparse_tag,
                reparse_data=info.reparse_data,
                security_access_requested=True,
                system_security_requested=request.policy.capture_system_acl,
                ea_access_requested=True,
                shared_write=request.policy.windows_allow_shared_write,
                shared_delete=request.policy.windows_allow_shared_delete,
                capture_usn=request.policy.windows_capture_usn,
            ),
            [],
        )

    def snapshot(self, fd: int, *, capture_usn: bool = True):
        self.snapshot_capture_usn.append(capture_usn)
        st = os.fstat(fd)
        usn = None
        if capture_usn:
            raw = b"mock-usn-record"
            usn = {
                "raw": raw,
                "record_length": len(raw),
                "major_version": 2,
                "minor_version": 0,
                "parse_status": "parsed",
                "file_reference_number": "0000000000000001",
                "parent_file_reference_number": "0000000000000002",
                "usn": "42",
                "security_id": 7,
                "file_attributes": 0x20,
                "file_name": "payload.bin",
                "file_name_utf16le": "payload.bin".encode("utf-16le"),
            }
        return WindowsSnapshot(
            identity=self._identity(fd=fd),
            creation_ticks=_ticks(st.st_ctime_ns),
            access_ticks=_ticks(st.st_atime_ns),
            write_ticks=_ticks(st.st_mtime_ns),
            change_ticks=_ticks(st.st_ctime_ns),
            attributes=0x20,
            reparse_tag=self.reparse_tag,
            allocation_size=((st.st_size + 4095) // 4096) * 4096,
            end_of_file=st.st_size,
            number_of_links=st.st_nlink,
            delete_pending=False,
            directory=False,
            file_index_64=st.st_ino & ((1 << 64) - 1),
            compressed_size=st.st_size,
            compression_format=0,
            compression_unit_shift=0,
            chunk_shift=0,
            cluster_shift=12,
            storage={
                "logical_bytes_per_sector": 512,
                "physical_bytes_per_sector_for_atomicity": 4096,
                "physical_bytes_per_sector_for_performance": 4096,
                "filesystem_effective_physical_bytes_per_sector_for_atomicity": 4096,
                "flags": 0,
                "byte_offset_for_sector_alignment": 0,
                "byte_offset_for_partition_alignment": 0,
            },
            usn=usn,
        )

    @staticmethod
    def security_descriptor(
        fd: int, *, include_sacl: bool, resolve_principals: bool
    ) -> SecurityDescriptorCapture:
        return SecurityDescriptorCapture(
            raw=b"self-relative-security-descriptor",
            security_information=0xF if include_sacl else 0x7,
            owner=WindowsPrincipal("S-1-5-21-1000", "EXAMPLE\\alice"),
            group=WindowsPrincipal("S-1-5-32-545", "BUILTIN\\Users"),
            sddl="O:S-1-5-21-1000G:BUILTIN\\UsersD:(A;;FR;;;WD)",
            control=0x8004,
            sacl_included=include_sacl,
        )

    @staticmethod
    def backup_streams(fd: int, request: ObservationRequest):
        first = _ea_entry(b"Archive.Source", b"host-a", flags=0x80, final=False)
        second = _ea_entry(b"Archive.Sequence", b"7", final=True)
        ea_data = first + second
        return [
            BackupStreamCapture(
                stream_id=BACKUP_EA_DATA,
                attributes=0,
                name="",
                name_bytes=b"",
                size=len(ea_data),
                capture_status="captured",
                data=ea_data,
                sha256=hashlib.sha256(ea_data).hexdigest(),
            ),
            BackupStreamCapture(
                stream_id=BACKUP_ALTERNATE_DATA,
                attributes=0,
                name=":Zone.Identifier:$DATA",
                name_bytes=":Zone.Identifier:$DATA".encode("utf-16le"),
                size=24,
                capture_status="captured",
                data=b"[ZoneTransfer]\r\nZoneId=3",
                sha256=hashlib.sha256(b"[ZoneTransfer]\r\nZoneId=3").hexdigest(),
            ),
        ]

    @staticmethod
    def allocated_ranges(fd: int, size: int, *, maximum_extents: int):
        return ([(0, size)] if size else [], True)

    @staticmethod
    def object_id(fd: int):
        return bytes(range(64))

    @staticmethod
    def integrity_info(fd: int):
        return IntegrityInfo(
            checksum_algorithm=2,
            flags=1,
            checksum_chunk_size=65536,
            cluster_size=4096,
        )

    @staticmethod
    def volume_info(fd: int, path: str):
        return WindowsVolumeInfo(
            filesystem_name="NTFS",
            volume_label="Archive",
            volume_serial_number=0xA1B2C3D4,
            maximum_component_length=255,
            filesystem_flags=0x0000000F | 0x00040000 | 0x00800000 | 0x02000000,
            mount_path="C:\\",
            volume_guid_path="\\\\?\\Volume{12345678-1234-5678-1234-567812345678}\\",
            drive_type=3,
            sectors_per_cluster=8,
            bytes_per_sector=512,
            final_path="\\\\?\\C:\\Archive\\payload.bin",
        )

    @staticmethod
    def os_information():
        return {
            "name": "Microsoft Windows 11 Pro",
            "version": "25H2",
            "build": "10.0.26200.8875",
            "edition": "Professional",
            "installation_type": "Client",
            "kernel_release": "10.0.26200",
            "kernel_version": "10.0.26200.8875",
        }

    @staticmethod
    def current_token(*, resolve: bool):
        return WindowsPrincipal("S-1-5-21-1000", "EXAMPLE\\alice"), "elevated"


def test_windows_abi_structure_sizes_are_fixed_width() -> None:
    assert ctypes.sizeof(_FILE_BASIC_INFO) == 40
    assert ctypes.sizeof(_FILE_STANDARD_INFO) == 24
    assert ctypes.sizeof(_FILE_ID_INFO) == 24
    assert ctypes.sizeof(_FILE_ATTRIBUTE_TAG_INFO) == 8
    assert ctypes.sizeof(_FILE_COMPRESSION_INFO) == 16
    assert ctypes.sizeof(_FILE_STORAGE_INFO) == 28
    assert ctypes.sizeof(_BY_HANDLE_FILE_INFORMATION) == 52
    assert ctypes.sizeof(_FILE_ALLOCATED_RANGE_BUFFER) == 16
    assert ctypes.sizeof(_TOKEN_ELEVATION) == 4


def test_mocked_windows_observation_contract(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "payload.bin"
    content = b"opaque bytes; payload internals are never interpreted\x00\xff"
    payload.write_bytes(content)
    native = FakeWindowsNative()
    result = WindowsFileStateObserver(native=native, enforce_platform=False).observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=urn_factory(),
            payload_binding=PayloadBindingRequest(relative_path="payload.bin"),
            policy=ObservationPolicy(second_content_hash=True),
        )
    )
    fragment = result.graph_fragment()
    validate_graph_fragment(fragment)

    assert result.state["content"]["size_bytes"] == len(content)
    assert result.state["content"]["digests"][0]["value"] == hashlib.sha256(content).hexdigest()
    assert result.state["locator"]["syntax"] == "windows"
    assert result.state["locator"]["source_encoding"] == "UTF-16LE"
    assert result.payload_binding is not None
    assert result.payload_binding["relative_payload_locator"]["syntax"] == "posix"

    access = result.state["filesystem_metadata"]["access"]
    assert access["owner"]["identifiers"][0]["value"] == "S-1-5-21-1000"
    rows = result.state["filesystem_metadata"]["native_metadata"]
    kinds = {row["kind"] for row in rows}
    assert {
        "security_descriptor",
        "windows_extended_attribute",
        "alternate_data_stream",
        "file_flag",
        "sparse_map",
        "compression_state",
        "encryption_state",
        "native_stat_field",
    }.issubset(kinds)
    ea_names = {row["name"] for row in rows if row["kind"] == "windows_extended_attribute"}
    assert ea_names == {"Archive.Source", "Archive.Sequence"}
    assert any(row["name"] == "change-journal-file-record" for row in rows)

    assert all(
        item["raw_unit"] == "ticks_100ns"
        for item in result.state["filesystem_metadata"]["timestamps"]
    )
    assert result.environment["operating_system"]["family"] == "windows"
    assert result.environment["operating_system"]["version"] == "25H2"
    assert result.environment["filesystem"]["type"] == "ntfs"
    assert result.capture["consistency"] == "verified_unchanged"
    assert result.capture["outcome"] == "success"


def test_windows_usn_policy_disables_native_query(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "no-usn.dat"
    payload.write_bytes(b"payload")
    native = FakeWindowsNative()
    result = WindowsFileStateObserver(native=native, enforce_platform=False).observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=urn_factory(),
            policy=ObservationPolicy(windows_capture_usn=False),
        )
    )
    assert native.snapshot_capture_usn and not any(native.snapshot_capture_usn)
    assert not any(
        row["name"] == "change-journal-file-record"
        for row in result.state["filesystem_metadata"]["native_metadata"]
    )
    validate_graph_fragment(result.graph_fragment())


def test_windows_locator_preserves_unpaired_utf16_code_unit() -> None:
    locator = windows_locator("C:\\Archive\\bad-\udcff.bin", kind="absolute")
    assert locator["syntax"] == "windows"
    assert locator["text_role"] == "display"
    assert locator["text"].startswith("utf16le:")
    raw = base64.b64decode(locator["bytes"]["data"])
    assert raw.decode("utf-16le", "surrogatepass") == "C:\\Archive\\bad-\udcff.bin"


def test_windows_name_surrogate_reparse_is_refused(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "reparse.dat"
    payload.write_bytes(b"payload")
    native = FakeWindowsNative()
    native.reparse_tag = 0xA000000C  # IO_REPARSE_TAG_SYMLINK; name-surrogate bit set.
    native.reparse_data = b"reparse"
    with pytest.raises(SymlinkRefusedError):
        WindowsFileStateObserver(native=native, enforce_platform=False).observe(
            ObservationRequest(
                path=payload,
                lineage_id=urn_factory(),
                host_id=urn_factory(),
            )
        )


def test_windows_drive_relative_payload_binding_is_rejected(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "binding.dat"
    payload.write_bytes(b"payload")
    with pytest.raises(ValueError, match="payload binding path must be relative"):
        WindowsFileStateObserver(native=FakeWindowsNative(), enforce_platform=False).observe(
            ObservationRequest(
                path=payload,
                lineage_id=urn_factory(),
                host_id=urn_factory(),
                payload_binding=PayloadBindingRequest(relative_path="C:binding.dat"),
            )
        )


def test_file_id_info_fallback_is_not_mislabeled() -> None:
    native = object.__new__(WindowsNativeAPI)
    legacy = _BY_HANDLE_FILE_INFORMATION()
    legacy.dwVolumeSerialNumber = 0x12345678
    legacy.nFileIndexHigh = 0x01020304
    legacy.nFileIndexLow = 0x05060708

    def unsupported(*args, **kwargs):
        exc = OSError(87, "unsupported")
        exc.winerror = 87
        raise exc

    native._get_info = unsupported  # type: ignore[method-assign]
    native._legacy_info = lambda handle: legacy  # type: ignore[method-assign]
    identity = native._identity_for_handle(123)
    assert identity.scheme == "windows-file-index-64"
    assert identity.file_id_bits == 64
    assert identity.source_api == "GetFileInformationByHandle"
    assert int.from_bytes(identity.file_id, "big") == 0x0102030405060708


def test_read_file_usn_data_omits_documented_invalid_fields() -> None:
    # USN_RECORD_V2 with deliberately nonzero bytes in TimeStamp, Reason, and
    # SourceInfo. The parser must not publish those fields for this FSCTL.
    name = "x.dat".encode("utf-16le")
    record_length = 60 + len(name)
    raw = bytearray(record_length)
    struct.pack_into("<IHH", raw, 0, record_length, 2, 0)
    struct.pack_into("<Q", raw, 8, 1)
    struct.pack_into("<Q", raw, 16, 2)
    struct.pack_into("<q", raw, 24, 99)
    struct.pack_into("<q", raw, 32, 123456789)  # invalid for this FSCTL
    struct.pack_into("<I", raw, 40, 0xFFFFFFFF)  # invalid Reason
    struct.pack_into("<I", raw, 44, 0xFFFFFFFF)  # invalid SourceInfo
    struct.pack_into("<I", raw, 48, 7)
    struct.pack_into("<I", raw, 52, 0x20)
    struct.pack_into("<HH", raw, 56, len(name), 60)
    raw[60:] = name

    native = object.__new__(WindowsNativeAPI)
    native._handle_from_fd = lambda fd: 1  # type: ignore[method-assign]
    native._device_io = lambda *args, **kwargs: (bytes(raw), None)  # type: ignore[method-assign]
    parsed = native.read_usn(3)
    assert parsed is not None
    assert parsed["usn"] == "99"
    assert parsed["file_name"] == "x.dat"
    assert "timestamp" not in parsed
    assert "reason" not in parsed
    assert "source_info" not in parsed


def test_ea_parser_honors_longword_aligned_chain(urn_factory) -> None:
    backend = WindowsBackend(native=FakeWindowsNative(), enforce_platform=False)
    request = ObservationRequest(
        path="unused",
        lineage_id=urn_factory(),
        host_id=urn_factory(),
    )
    data = _ea_entry(b"A", b"one", final=False) + _ea_entry(b"B", b"two", final=True)
    rows = backend._parse_ea_stream(data, request)
    assert [row["name"] for row in rows] == ["A", "B"]
    assert [base64.b64decode(row["value"]["data"]) for row in rows] == [
        b"one",
        b"two",
    ]
