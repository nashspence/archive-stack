"""Archive-grade Windows regular-file state observer.

The module remains importable on non-Windows hosts so its ABI definitions,
serialization, and backend mapping can be tested with API doubles.  Native calls
are loaded only when :class:`WindowsNativeAPI` is instantiated on Windows.
"""

from __future__ import annotations

import base64
import ctypes
import datetime as dt
import hashlib
import locale
import os
import platform
import socket
import stat as statmod
import struct
import sys
import uuid
from collections.abc import Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, TypeVar

from riverhog_provenance.common import (
    DescriptorFileStateObserver,
    bytes_value,
    diagnostic,
    digest_assertion,
    identifier,
    merge_coverage,
    observed_identifier,
    retained_native_value,
    safe_portable_text,
    source,
    utc_offset_string,
)
from riverhog_provenance.constants import DEFAULT_OBSERVER_AGENT_ID, OBSERVER_NAMESPACE
from riverhog_provenance.errors import (
    NativeObservationError,
    SymlinkRefusedError,
    UnsupportedFileTypeError,
    UnsupportedPlatformError,
)
from riverhog_provenance.interface import PlatformBackend
from riverhog_provenance.model import (
    ExtensionDraft,
    JsonObject,
    LargeValueDisposition,
    NativeCollection,
    NativeStat,
    ObservationRequest,
    PathInput,
)
from riverhog_provenance_windows_contracts import PLATFORM_FAMILY

# Fixed-width Windows ABI scalar types.  ctypes.wintypes.DWORD is host-ABI
# dependent on non-Windows LP64 builds, which would invalidate structure tests.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_uint8
_BOOL = ctypes.c_int32
_LONG64 = ctypes.c_int64
_ULONG64 = ctypes.c_uint64
_UINT = ctypes.c_uint32
_TStructure = TypeVar("_TStructure", bound=ctypes.Structure)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# CreateFile access/share/disposition/flag constants.
GENERIC_READ = 0x80000000
ACCESS_SYSTEM_SECURITY = 0x01000000
FILE_READ_DATA = 0x0001
FILE_READ_EA = 0x0008
FILE_READ_ATTRIBUTES = 0x0080
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

# File attributes.
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_SPARSE_FILE = 0x00000200
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_COMPRESSED = 0x00000800
FILE_ATTRIBUTE_ENCRYPTED = 0x00004000
FILE_ATTRIBUTE_EA = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_STRICTLY_SEQUENTIAL = 0x20000000

FILE_ATTRIBUTE_NAMES: tuple[tuple[int, str], ...] = (
    (0x00000001, "readonly"),
    (0x00000002, "hidden"),
    (0x00000004, "system"),
    (0x00000010, "directory"),
    (0x00000020, "archive"),
    (0x00000040, "device"),
    (0x00000080, "normal"),
    (0x00000100, "temporary"),
    (0x00000200, "sparse_file"),
    (0x00000400, "reparse_point"),
    (0x00000800, "compressed"),
    (0x00001000, "offline"),
    (0x00002000, "not_content_indexed"),
    (0x00004000, "encrypted"),
    (0x00008000, "integrity_stream"),
    (0x00010000, "virtual"),
    (0x00020000, "no_scrub_data"),
    (0x00040000, "extended_attributes_or_recall_on_open"),
    (0x00080000, "pinned"),
    (0x00100000, "unpinned"),
    (0x00400000, "recall_on_data_access"),
    (0x20000000, "strictly_sequential"),
)

# GetFileInformationByHandleEx classes used here.
FILE_BASIC_INFO_CLASS = 0
FILE_STANDARD_INFO_CLASS = 1
FILE_COMPRESSION_INFO_CLASS = 8
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_STORAGE_INFO_CLASS = 16
FILE_ID_INFO_CLASS = 18

# Security information and token constants.
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
DACL_SECURITY_INFORMATION = 0x00000004
SACL_SECURITY_INFORMATION = 0x00000008
LABEL_SECURITY_INFORMATION = 0x00000010
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1
TOKEN_ELEVATION_CLASS = 20
TOKEN_ELEVATION_TYPE_CLASS = 18
TOKEN_ELEVATION_TYPE_FULL = 2

# BackupRead stream IDs/attributes.
BACKUP_DATA = 0x00000001
BACKUP_EA_DATA = 0x00000002
BACKUP_SECURITY_DATA = 0x00000003
BACKUP_ALTERNATE_DATA = 0x00000004
BACKUP_LINK = 0x00000005
BACKUP_PROPERTY_DATA = 0x00000006
BACKUP_OBJECT_ID = 0x00000007
BACKUP_REPARSE_DATA = 0x00000008
BACKUP_SPARSE_BLOCK = 0x00000009
BACKUP_TXFS_DATA = 0x0000000A
STREAM_MODIFIED_WHEN_READ = 0x00000001
STREAM_CONTAINS_SECURITY = 0x00000002
_BACKUP_HEADER = struct.Struct("<IIqI")

# DeviceIoControl codes.
FSCTL_GET_OBJECT_ID = 0x0009009C
FSCTL_GET_REPARSE_POINT = 0x000900A8
FSCTL_READ_FILE_USN_DATA = 0x000900EB
FSCTL_QUERY_ALLOCATED_RANGES = 0x000940CF
FSCTL_GET_INTEGRITY_INFORMATION = 0x0009027C

# Win32 errors handled as unsupported/absent rather than fatal.
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_FUNCTION = 1
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_INVALID_PARAMETER = 87
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234
ERROR_NOT_SUPPORTED = 50
ERROR_PRIVILEGE_NOT_HELD = 1314
ERROR_OBJECT_NOT_FOUND = 4312
ERROR_NONE_MAPPED = 1332
ERROR_NOT_A_REPARSE_POINT = 4390
ERROR_HANDLE_EOF = 38

# Volume flags and drive types.
FILE_CASE_SENSITIVE_SEARCH = 0x00000001
FILE_CASE_PRESERVED_NAMES = 0x00000002
FILE_UNICODE_ON_DISK = 0x00000004
FILE_PERSISTENT_ACLS = 0x00000008
FILE_NAMED_STREAMS = 0x00040000
FILE_SUPPORTS_EXTENDED_ATTRIBUTES = 0x00800000
FILE_SUPPORTS_USN_JOURNAL = 0x02000000
DRIVE_REMOTE = 4

COMPRESSION_FORMAT_NAMES = {
    0: "none",
    1: "default",
    2: "lznt1",
    3: "xpress",
    4: "xpress_huff",
}

FILETIME_UNIX_EPOCH_TICKS = 116_444_736_000_000_000
FILETIME_EPOCH_TEXT = "1601-01-01T00:00:00Z"


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD)]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", _LONG64),
        ("LastAccessTime", _LONG64),
        ("LastWriteTime", _LONG64),
        ("ChangeTime", _LONG64),
        ("FileAttributes", _DWORD),
    ]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", _LONG64),
        ("EndOfFile", _LONG64),
        ("NumberOfLinks", _DWORD),
        ("DeletePending", _BYTE),
        ("Directory", _BYTE),
    ]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", _BYTE * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", _ULONG64), ("FileId", _FILE_ID_128)]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", _DWORD), ("ReparseTag", _DWORD)]


class _FILE_COMPRESSION_INFO(ctypes.Structure):
    _fields_ = [
        ("CompressedFileSize", _LONG64),
        ("CompressionFormat", _WORD),
        ("CompressionUnitShift", _BYTE),
        ("ChunkShift", _BYTE),
        ("ClusterShift", _BYTE),
        ("Reserved", _BYTE * 3),
    ]


class _FILE_STORAGE_INFO(ctypes.Structure):
    _fields_ = [
        ("LogicalBytesPerSector", _DWORD),
        ("PhysicalBytesPerSectorForAtomicity", _DWORD),
        ("PhysicalBytesPerSectorForPerformance", _DWORD),
        ("FileSystemEffectivePhysicalBytesPerSectorForAtomicity", _DWORD),
        ("Flags", _DWORD),
        ("ByteOffsetForSectorAlignment", _DWORD),
        ("ByteOffsetForPartitionAlignment", _DWORD),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", _DWORD),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", _DWORD),
        ("nFileSizeHigh", _DWORD),
        ("nFileSizeLow", _DWORD),
        ("nNumberOfLinks", _DWORD),
        ("nFileIndexHigh", _DWORD),
        ("nFileIndexLow", _DWORD),
    ]


class _FILE_ALLOCATED_RANGE_BUFFER(ctypes.Structure):
    _fields_ = [("FileOffset", _LONG64), ("Length", _LONG64)]


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", _DWORD)]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", _DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _FSCTL_GET_INTEGRITY_INFORMATION_BUFFER(ctypes.Structure):
    _fields_ = [
        ("ChecksumAlgorithm", _WORD),
        ("Reserved", _WORD),
        ("Flags", _DWORD),
        ("ChecksumChunkSizeInBytes", _DWORD),
        ("ClusterSizeInBytes", _DWORD),
    ]


@dataclass(frozen=True, slots=True)
class WindowsIdentity:
    """Volume-scoped file identity and the API that supplied it."""

    volume_serial_number: int
    file_id: bytes
    scheme: str = "windows-file-id-128"
    source_api: str = "GetFileInformationByHandleEx"
    source_field: str = "FileIdInfo.FileId"

    @property
    def file_id_hex(self) -> str:
        return self.file_id.hex()

    @property
    def file_id_bits(self) -> int:
        return len(self.file_id) * 8


@dataclass(frozen=True, slots=True)
class WindowsPathInfo:
    identity: WindowsIdentity
    attributes: int
    reparse_tag: int
    directory: bool
    reparse_data: bytes | None = None


@dataclass(frozen=True, slots=True)
class WindowsSnapshot:
    identity: WindowsIdentity
    creation_ticks: int
    access_ticks: int
    write_ticks: int
    change_ticks: int
    attributes: int
    reparse_tag: int
    allocation_size: int
    end_of_file: int
    number_of_links: int
    delete_pending: bool
    directory: bool
    file_index_64: int
    compressed_size: int | None = None
    compression_format: int | None = None
    compression_unit_shift: int | None = None
    chunk_shift: int | None = None
    cluster_shift: int | None = None
    storage: Mapping[str, int] | None = None
    usn: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class OpenedWindowsFile:
    fd: int
    identity: WindowsIdentity
    attributes: int
    reparse_tag: int
    reparse_data: bytes | None
    security_access_requested: bool
    system_security_requested: bool
    ea_access_requested: bool
    shared_write: bool
    capture_usn: bool
    shared_delete: bool = False


@dataclass(frozen=True, slots=True)
class WindowsPrincipal:
    sid: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityDescriptorCapture:
    raw: bytes
    security_information: int
    owner: WindowsPrincipal | None
    group: WindowsPrincipal | None
    sddl: str | None
    control: int | None
    sacl_included: bool


@dataclass(frozen=True, slots=True)
class BackupStreamCapture:
    stream_id: int
    attributes: int
    name: str
    name_bytes: bytes
    size: int
    capture_status: str
    data: bytes | None = None
    sha256: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class WindowsVolumeInfo:
    filesystem_name: str
    volume_label: str
    volume_serial_number: int
    maximum_component_length: int
    filesystem_flags: int
    mount_path: str | None
    volume_guid_path: str | None
    drive_type: int | None
    sectors_per_cluster: int | None
    bytes_per_sector: int | None
    final_path: str | None


@dataclass(frozen=True, slots=True)
class IntegrityInfo:
    checksum_algorithm: int
    flags: int
    checksum_chunk_size: int
    cluster_size: int


def _is_name_surrogate(tag: int) -> bool:
    # IsReparseTagNameSurrogate from ntifs.h.
    return bool(tag & 0x20000000)


def _is_unsupported_winerror(error: int) -> bool:
    return error in {
        ERROR_INVALID_FUNCTION,
        ERROR_INVALID_PARAMETER,
        ERROR_NOT_SUPPORTED,
    }


def _winerror(error: int, message: str) -> OSError:
    try:
        detail = ctypes.FormatError(error)
    except Exception:
        detail = f"Win32 error {error}"
    exc = OSError(error, f"{message}: {detail}")
    exc.winerror = error
    return exc


def _filetime_to_unix_ns(ticks: int) -> int:
    return (ticks - FILETIME_UNIX_EPOCH_TICKS) * 100


def _format_filetime(ticks: int) -> str:
    if ticks <= 0:
        raise ValueError("FILETIME value is not a positive absolute timestamp")
    unix_100ns = ticks - FILETIME_UNIX_EPOCH_TICKS
    seconds, remainder = divmod(unix_100ns, 10_000_000)
    moment = dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if remainder:
        return f"{base}.{remainder:07d}".rstrip("0") + "Z"
    return base + "Z"


def _filetime_observation(kind: str, ticks: int, field: str) -> JsonObject:
    if ticks <= 0:
        return {
            "kind": kind,
            "value_status": "unresolved",
            "source": source("windows", "GetFileInformationByHandleEx", field),
            "raw_value": str(ticks),
            "raw_unit": "ticks_100ns",
            "raw_epoch": FILETIME_EPOCH_TEXT,
        }
    return {
        "kind": kind,
        "value_status": "exact",
        "value": _format_filetime(ticks),
        "resolution_ns": 100,
        "source": source("windows", "GetFileInformationByHandleEx", field),
        "raw_value": str(ticks),
        "raw_unit": "ticks_100ns",
        "raw_epoch": FILETIME_EPOCH_TEXT,
    }


def _portable_windows_text(value: str) -> tuple[str, str]:
    try:
        return safe_portable_text(value), "exact"
    except ValueError:
        encoded = value.encode("utf-16le", "surrogatepass")
        return "utf16le:" + encoded.hex(), "display"


def windows_locator(
    path: str,
    *,
    kind: str,
    authority_id: str | None = None,
) -> JsonObject:
    raw = path.encode("utf-16le", "surrogatepass")
    text, role = _portable_windows_text(path)
    result: JsonObject = {
        "syntax": "windows",
        "kind": kind,
        "text": text,
        "bytes": {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
            "byte_length": len(raw),
        },
        "text_role": role,
    }
    if role == "exact":
        result["source_encoding"] = "UTF-16LE"
    if authority_id is not None:
        result["authority_id"] = authority_id
    return result


def _windows_name_fields(name: str) -> JsonObject:
    raw = name.encode("utf-16le", "surrogatepass")
    text, role = _portable_windows_text(name)
    result: JsonObject = {
        "name": text,
        "name_bytes": {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
            "byte_length": len(raw),
        },
        "name_role": role,
    }
    if role == "exact":
        result["name_source_encoding"] = "UTF-16LE"
    return result


def _attribute_names(value: int) -> list[str]:
    return [name for bit, name in FILE_ATTRIBUTE_NAMES if value & bit]


def _stream_attribute_names(value: int) -> list[str]:
    names: list[str] = []
    if value & STREAM_MODIFIED_WHEN_READ:
        names.append("modified_when_read")
    if value & STREAM_CONTAINS_SECURITY:
        names.append("contains_security")
    return names


def _principal_document(principal: WindowsPrincipal | None, kind: str) -> JsonObject | None:
    if principal is None:
        return None
    result: JsonObject = {
        "kind": kind,
        "identifiers": [identifier(scheme="windows-sid", value=principal.sid, scope="global")],
        "resolution": "resolved" if principal.name else "unresolved",
    }
    if principal.name:
        result["name"] = safe_portable_text(principal.name)
    return result


def _schema_value(schema_name: str, data: Mapping[str, Any]) -> JsonObject:
    uri = (
        f"https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/{schema_name}.json"
    )
    return {"type": "json", "schema": uri, "data": dict(data)}


def _native_row(
    *,
    kind: str,
    category: str,
    name: str,
    api: str,
    field: str,
    value: JsonObject | None,
    status: str = "captured",
    namespace: str = "windows",
    sensitivity: str = "public",
    note: str | None = None,
    interpretations: Sequence[JsonObject] = (),
    observed_byte_length: int | None = None,
) -> JsonObject:
    row: JsonObject = {
        "kind": kind,
        "coverage_category": category,
        "namespace": namespace,
        "name": name,
        "capture_status": status,
        "source": source("windows", api, field),
        "sensitivity": sensitivity,
    }
    if value is not None:
        row["value"] = value
    if note:
        row["note"] = note
    if interpretations:
        row["interpretations"] = list(interpretations)
    if observed_byte_length is not None:
        row["observed_byte_length"] = observed_byte_length
    return row


class WindowsNativeAPI:
    """Thin, descriptor-oriented Win32 API adapter.

    The adapter deliberately exposes Python dataclasses rather than ctypes
    structures so that the backend can be tested with API doubles on non-Windows
    hosts.  Handles converted with ``msvcrt.open_osfhandle`` are owned by the
    resulting Python file descriptor and must be closed only with ``os.close``.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise UnsupportedPlatformError("WindowsNativeAPI requires Windows")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:  # pragma: no cover - defensive on unusual runtimes
            raise UnsupportedPlatformError("ctypes WinDLL support is unavailable")
        self.kernel32: Any = loader("kernel32", use_last_error=True)
        self.advapi32: Any = loader("advapi32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        k32 = self.kernel32
        adv = self.advapi32
        handle = wintypes.HANDLE
        lpvoid = wintypes.LPVOID
        lpdword = ctypes.POINTER(_DWORD)

        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            _DWORD,
            _DWORD,
            lpvoid,
            _DWORD,
            _DWORD,
            handle,
        ]
        k32.CreateFileW.restype = handle
        k32.CloseHandle.argtypes = [handle]
        k32.CloseHandle.restype = _BOOL
        k32.GetFileInformationByHandleEx.argtypes = [
            handle,
            ctypes.c_int,
            lpvoid,
            _DWORD,
        ]
        k32.GetFileInformationByHandleEx.restype = _BOOL
        k32.GetFileInformationByHandle.argtypes = [
            handle,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        k32.GetFileInformationByHandle.restype = _BOOL
        k32.DeviceIoControl.argtypes = [
            handle,
            _DWORD,
            lpvoid,
            _DWORD,
            lpvoid,
            _DWORD,
            lpdword,
            lpvoid,
        ]
        k32.DeviceIoControl.restype = _BOOL
        k32.BackupRead.argtypes = [
            handle,
            ctypes.POINTER(_BYTE),
            _DWORD,
            lpdword,
            _BOOL,
            _BOOL,
            ctypes.POINTER(lpvoid),
        ]
        k32.BackupRead.restype = _BOOL
        k32.BackupSeek.argtypes = [
            handle,
            _DWORD,
            _DWORD,
            lpdword,
            lpdword,
            ctypes.POINTER(lpvoid),
        ]
        k32.BackupSeek.restype = _BOOL
        k32.GetVolumeInformationByHandleW.argtypes = [
            handle,
            wintypes.LPWSTR,
            _DWORD,
            lpdword,
            lpdword,
            lpdword,
            wintypes.LPWSTR,
            _DWORD,
        ]
        k32.GetVolumeInformationByHandleW.restype = _BOOL
        k32.GetFinalPathNameByHandleW.argtypes = [
            handle,
            wintypes.LPWSTR,
            _DWORD,
            _DWORD,
        ]
        k32.GetFinalPathNameByHandleW.restype = _DWORD
        k32.GetVolumePathNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            _DWORD,
        ]
        k32.GetVolumePathNameW.restype = _BOOL
        k32.GetVolumeNameForVolumeMountPointW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            _DWORD,
        ]
        k32.GetVolumeNameForVolumeMountPointW.restype = _BOOL
        k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        k32.GetDriveTypeW.restype = _UINT
        k32.GetDiskFreeSpaceW.argtypes = [
            wintypes.LPCWSTR,
            lpdword,
            lpdword,
            lpdword,
            lpdword,
        ]
        k32.GetDiskFreeSpaceW.restype = _BOOL
        k32.LocalFree.argtypes = [lpvoid]
        k32.LocalFree.restype = lpvoid
        k32.GetCurrentProcess.argtypes = []
        k32.GetCurrentProcess.restype = handle

        adv.OpenProcessToken.argtypes = [handle, _DWORD, ctypes.POINTER(handle)]
        adv.OpenProcessToken.restype = _BOOL
        adv.GetTokenInformation.argtypes = [
            handle,
            ctypes.c_int,
            lpvoid,
            _DWORD,
            lpdword,
        ]
        adv.GetTokenInformation.restype = _BOOL
        adv.GetKernelObjectSecurity.argtypes = [
            handle,
            _DWORD,
            lpvoid,
            _DWORD,
            lpdword,
        ]
        adv.GetKernelObjectSecurity.restype = _BOOL
        adv.GetSecurityDescriptorOwner.argtypes = [
            lpvoid,
            ctypes.POINTER(lpvoid),
            ctypes.POINTER(_BOOL),
        ]
        adv.GetSecurityDescriptorOwner.restype = _BOOL
        adv.GetSecurityDescriptorGroup.argtypes = [
            lpvoid,
            ctypes.POINTER(lpvoid),
            ctypes.POINTER(_BOOL),
        ]
        adv.GetSecurityDescriptorGroup.restype = _BOOL
        adv.GetSecurityDescriptorControl.argtypes = [
            lpvoid,
            ctypes.POINTER(_WORD),
            lpdword,
        ]
        adv.GetSecurityDescriptorControl.restype = _BOOL
        adv.ConvertSidToStringSidW.argtypes = [lpvoid, ctypes.POINTER(wintypes.LPWSTR)]
        adv.ConvertSidToStringSidW.restype = _BOOL
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            lpvoid,
            _DWORD,
            _DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            lpdword,
        ]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = _BOOL
        adv.LookupAccountSidW.argtypes = [
            wintypes.LPCWSTR,
            lpvoid,
            wintypes.LPWSTR,
            lpdword,
            wintypes.LPWSTR,
            lpdword,
            lpdword,
        ]
        adv.LookupAccountSidW.restype = _BOOL

    @staticmethod
    def _handle_from_fd(fd: int) -> int:
        import msvcrt

        value = int(msvcrt.get_osfhandle(fd))
        if value == -1:
            raise OSError("invalid Windows CRT file descriptor")
        return value

    @staticmethod
    def _fd_from_handle(handle: int) -> int:
        import msvcrt

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            return int(msvcrt.open_osfhandle(handle, flags))
        except Exception:
            # Conversion transfers ownership only on success.
            raise

    def _close_handle(self, handle: int) -> None:
        if handle not in {0, INVALID_HANDLE_VALUE}:
            self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def _create_handle(
        self,
        path: str,
        *,
        desired_access: int,
        share_mode: int,
        flags: int,
    ) -> int:
        ctypes.set_last_error(0)
        raw = self.kernel32.CreateFileW(
            path,
            desired_access,
            share_mode,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        value = ctypes.cast(raw, ctypes.c_void_p).value
        if value in {None, INVALID_HANDLE_VALUE}:
            error = ctypes.get_last_error()
            raise _winerror(error, f"CreateFileW failed for {path!r}")
        return int(value)

    def _get_info(
        self,
        handle: int,
        info_class: int,
        struct_type: type[_TStructure],
    ) -> _TStructure:
        value = struct_type()
        ctypes.set_last_error(0)
        ok = self.kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle),
            info_class,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if not ok:
            error = ctypes.get_last_error()
            raise _winerror(error, f"GetFileInformationByHandleEx({info_class}) failed")
        return value

    def _legacy_info(self, handle: int) -> _BY_HANDLE_FILE_INFORMATION:
        value = _BY_HANDLE_FILE_INFORMATION()
        ctypes.set_last_error(0)
        if not self.kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(handle), ctypes.byref(value)
        ):
            raise _winerror(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        return value

    def _identity_for_handle(
        self,
        handle: int,
        legacy: _BY_HANDLE_FILE_INFORMATION | None = None,
    ) -> WindowsIdentity:
        try:
            info = self._get_info(handle, FILE_ID_INFO_CLASS, _FILE_ID_INFO)
        except OSError as exc:
            if not _is_unsupported_winerror(int(getattr(exc, "winerror", exc.errno or 0))):
                raise
            legacy = legacy or self._legacy_info(handle)
            index = (int(legacy.nFileIndexHigh) << 32) | int(legacy.nFileIndexLow)
            return WindowsIdentity(
                volume_serial_number=int(legacy.dwVolumeSerialNumber),
                file_id=index.to_bytes(8, "big", signed=False),
                scheme="windows-file-index-64",
                source_api="GetFileInformationByHandle",
                source_field="nFileIndexHigh:nFileIndexLow",
            )
        return WindowsIdentity(
            volume_serial_number=int(info.VolumeSerialNumber),
            file_id=bytes(info.FileId.Identifier),
        )

    def _device_io(
        self,
        handle: int,
        code: int,
        *,
        input_bytes: bytes | None = None,
        output_size: int,
        allow_more_data: bool = False,
    ) -> tuple[bytes, int | None]:
        in_buffer = None
        in_length = 0
        if input_bytes is not None:
            in_buffer = ctypes.create_string_buffer(input_bytes, len(input_bytes))
            in_length = len(input_bytes)
        out_buffer = ctypes.create_string_buffer(max(1, output_size))
        returned = _DWORD()
        ctypes.set_last_error(0)
        ok = self.kernel32.DeviceIoControl(
            wintypes.HANDLE(handle),
            code,
            ctypes.byref(in_buffer) if in_buffer is not None else None,
            in_length,
            ctypes.byref(out_buffer),
            output_size,
            ctypes.byref(returned),
            None,
        )
        error: int | None = None
        if not ok:
            error = ctypes.get_last_error()
            if not (allow_more_data and error == ERROR_MORE_DATA):
                raise _winerror(error, f"DeviceIoControl(0x{code:08x}) failed")
        return bytes(out_buffer.raw[: int(returned.value)]), error

    def reparse_data_for_handle(self, handle: int) -> bytes | None:
        try:
            data, _ = self._device_io(
                handle,
                FSCTL_GET_REPARSE_POINT,
                output_size=16 * 1024,
            )
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            if error == ERROR_NOT_A_REPARSE_POINT:
                return None
            raise
        return data

    def inspect_path(
        self,
        path: str,
        *,
        follow_non_name_surrogate: bool = True,
    ) -> WindowsPathInfo:
        share = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        flags = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
        inspection = self._create_handle(
            path,
            desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_mode=share,
            flags=flags,
        )
        try:
            legacy = self._legacy_info(inspection)
            standard = self._get_info(inspection, FILE_STANDARD_INFO_CLASS, _FILE_STANDARD_INFO)
            tag = self._get_info(
                inspection, FILE_ATTRIBUTE_TAG_INFO_CLASS, _FILE_ATTRIBUTE_TAG_INFO
            )
            identity = self._identity_for_handle(inspection, legacy)
            reparse_data = None
            if int(tag.FileAttributes) & FILE_ATTRIBUTE_REPARSE_POINT:
                reparse_data = self.reparse_data_for_handle(inspection)
                if follow_non_name_surrogate and not _is_name_surrogate(int(tag.ReparseTag)):
                    followed = self._create_handle(
                        path,
                        desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                        share_mode=share,
                        flags=FILE_FLAG_BACKUP_SEMANTICS,
                    )
                    try:
                        identity = self._identity_for_handle(followed)
                    finally:
                        self._close_handle(followed)
            return WindowsPathInfo(
                identity=identity,
                attributes=int(tag.FileAttributes),
                reparse_tag=int(tag.ReparseTag),
                directory=bool(standard.Directory),
                reparse_data=reparse_data,
            )
        finally:
            self._close_handle(inspection)

    def open_regular_file(
        self, path: str, request: ObservationRequest
    ) -> tuple[OpenedWindowsFile, list[JsonObject]]:
        diagnostics: list[JsonObject] = []
        path_info = self.inspect_path(
            path,
            follow_non_name_surrogate=request.policy.windows_follow_non_name_surrogate_reparse_points,
        )
        if path_info.directory:
            raise UnsupportedFileTypeError("target is a directory, not a regular file")
        if path_info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            if _is_name_surrogate(path_info.reparse_tag):
                raise SymlinkRefusedError("refusing to follow a name-surrogate reparse point")
            if not request.policy.windows_follow_non_name_surrogate_reparse_points:
                raise UnsupportedFileTypeError(
                    "non-name-surrogate reparse file requires explicit follow policy"
                )

        share = FILE_SHARE_READ
        if request.policy.windows_allow_shared_write:
            share |= FILE_SHARE_WRITE
            diagnostics.append(
                diagnostic(
                    severity="warning",
                    category="basic_filesystem",
                    code="windows_shared_write_enabled",
                    message=(
                        "The observer allowed concurrent writers. Stability is checked "
                        "after capture, but writer exclusion was not established."
                    ),
                    source_descriptor=source("windows", "CreateFileW", "dwShareMode"),
                )
            )
        if request.policy.windows_allow_shared_delete:
            share |= FILE_SHARE_DELETE
            diagnostics.append(
                diagnostic(
                    severity="warning",
                    category="locator",
                    code="windows_shared_delete_enabled",
                    message=(
                        "The observer allowed delete/rename sharing. Final path binding "
                        "is checked, but the name was not held against replacement."
                    ),
                    source_descriptor=source("windows", "CreateFileW", "dwShareMode"),
                )
            )

        flags = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_SEQUENTIAL_SCAN
        desired = GENERIC_READ
        system_security = False
        if request.policy.capture_system_acl:
            desired |= ACCESS_SYSTEM_SECURITY
        handle: int | None = None
        security_access = True
        ea_access = True
        try:
            try:
                handle = self._create_handle(
                    path, desired_access=desired, share_mode=share, flags=flags
                )
                system_security = bool(desired & ACCESS_SYSTEM_SECURITY)
            except OSError as first:
                first_error = int(getattr(first, "winerror", first.errno or 0))
                if request.policy.capture_system_acl and first_error in {
                    ERROR_ACCESS_DENIED,
                    ERROR_PRIVILEGE_NOT_HELD,
                }:
                    diagnostics.append(
                        diagnostic(
                            severity="error",
                            category="access_control",
                            code="windows_sacl_open_unavailable",
                            message=(
                                "The file could not be opened with ACCESS_SYSTEM_SECURITY; "
                                "capture continued without the SACL."
                            ),
                            native_code=str(first_error),
                            source_descriptor=source(
                                "windows", "CreateFileW", "ACCESS_SYSTEM_SECURITY"
                            ),
                        )
                    )
                    try:
                        handle = self._create_handle(
                            path,
                            desired_access=GENERIC_READ,
                            share_mode=share,
                            flags=flags,
                        )
                    except OSError as second:
                        first = second
                    else:
                        first = None  # type: ignore[assignment]
                if handle is None:
                    error = int(getattr(first, "winerror", first.errno or 0))
                    if error != ERROR_ACCESS_DENIED:
                        raise first
                    # Some ACLs grant data/attribute rights but not READ_CONTROL,
                    # which is part of GENERIC_READ. Preserve content fixity and
                    # mark security metadata incomplete rather than refusing the file.
                    explicit = FILE_READ_DATA | FILE_READ_EA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
                    try:
                        handle = self._create_handle(
                            path,
                            desired_access=explicit,
                            share_mode=share,
                            flags=flags,
                        )
                    except OSError as explicit_error:
                        explicit_code = int(
                            getattr(
                                explicit_error,
                                "winerror",
                                explicit_error.errno or 0,
                            )
                        )
                        if explicit_code != ERROR_ACCESS_DENIED:
                            raise
                        handle = self._create_handle(
                            path,
                            desired_access=(FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE),
                            share_mode=share,
                            flags=flags,
                        )
                        ea_access = False
                        diagnostics.append(
                            diagnostic(
                                severity="error",
                                category="extended_attributes",
                                code="windows_read_ea_unavailable",
                                message=(
                                    "Primary content was readable, but FILE_READ_EA was "
                                    "not; extended attributes may be unavailable."
                                ),
                                native_code=str(explicit_code),
                                source_descriptor=source("windows", "CreateFileW", "FILE_READ_EA"),
                            )
                        )
                    security_access = False
                    diagnostics.append(
                        diagnostic(
                            severity="error",
                            category="access_control",
                            code="windows_read_control_unavailable",
                            message=(
                                "Primary content was readable, but READ_CONTROL was not; "
                                "owner, group, and DACL capture may be unavailable."
                            ),
                            native_code=str(error),
                            source_descriptor=source("windows", "CreateFileW", "dwDesiredAccess"),
                        )
                    )

            assert handle is not None
            identity = self._identity_for_handle(handle)
            tag = self._get_info(handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, _FILE_ATTRIBUTE_TAG_INFO)
            standard = self._get_info(handle, FILE_STANDARD_INFO_CLASS, _FILE_STANDARD_INFO)
            if bool(standard.Directory):
                raise UnsupportedFileTypeError("opened target is a directory")
            if identity != path_info.identity:
                raise NativeObservationError(
                    "path identity changed between reparse inspection and content open"
                )
            fd = self._fd_from_handle(handle)
            handle = None  # fd now owns it
            return (
                OpenedWindowsFile(
                    fd=fd,
                    identity=identity,
                    attributes=int(tag.FileAttributes),
                    reparse_tag=path_info.reparse_tag,
                    reparse_data=path_info.reparse_data,
                    security_access_requested=security_access,
                    system_security_requested=system_security,
                    ea_access_requested=ea_access,
                    shared_write=request.policy.windows_allow_shared_write,
                    shared_delete=request.policy.windows_allow_shared_delete,
                    capture_usn=request.policy.windows_capture_usn,
                ),
                diagnostics,
            )
        finally:
            if handle is not None:
                self._close_handle(handle)

    def snapshot(self, fd: int, *, capture_usn: bool = True) -> WindowsSnapshot:
        handle = self._handle_from_fd(fd)
        basic = self._get_info(handle, FILE_BASIC_INFO_CLASS, _FILE_BASIC_INFO)
        standard = self._get_info(handle, FILE_STANDARD_INFO_CLASS, _FILE_STANDARD_INFO)
        tag = self._get_info(handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, _FILE_ATTRIBUTE_TAG_INFO)
        legacy = self._legacy_info(handle)
        identity = self._identity_for_handle(handle, legacy)
        compression: _FILE_COMPRESSION_INFO | None = None
        storage: _FILE_STORAGE_INFO | None = None
        try:
            compression = self._get_info(
                handle, FILE_COMPRESSION_INFO_CLASS, _FILE_COMPRESSION_INFO
            )
        except OSError as exc:
            if not _is_unsupported_winerror(int(getattr(exc, "winerror", exc.errno or 0))):
                raise
        try:
            storage = self._get_info(handle, FILE_STORAGE_INFO_CLASS, _FILE_STORAGE_INFO)
        except OSError as exc:
            if not _is_unsupported_winerror(int(getattr(exc, "winerror", exc.errno or 0))):
                raise
        usn = self.read_usn(fd) if capture_usn else None
        file_index = (int(legacy.nFileIndexHigh) << 32) | int(legacy.nFileIndexLow)
        storage_doc: dict[str, int] | None = None
        if storage is not None:
            storage_doc = {
                "logical_bytes_per_sector": int(storage.LogicalBytesPerSector),
                "physical_bytes_per_sector_for_atomicity": int(
                    storage.PhysicalBytesPerSectorForAtomicity
                ),
                "physical_bytes_per_sector_for_performance": int(
                    storage.PhysicalBytesPerSectorForPerformance
                ),
                "filesystem_effective_physical_bytes_per_sector_for_atomicity": int(
                    storage.FileSystemEffectivePhysicalBytesPerSectorForAtomicity
                ),
                "flags": int(storage.Flags),
                "byte_offset_for_sector_alignment": int(storage.ByteOffsetForSectorAlignment),
                "byte_offset_for_partition_alignment": int(storage.ByteOffsetForPartitionAlignment),
            }
        return WindowsSnapshot(
            identity=identity,
            creation_ticks=int(basic.CreationTime),
            access_ticks=int(basic.LastAccessTime),
            write_ticks=int(basic.LastWriteTime),
            change_ticks=int(basic.ChangeTime),
            attributes=int(tag.FileAttributes),
            reparse_tag=int(tag.ReparseTag),
            allocation_size=int(standard.AllocationSize),
            end_of_file=int(standard.EndOfFile),
            number_of_links=int(standard.NumberOfLinks),
            delete_pending=bool(standard.DeletePending),
            directory=bool(standard.Directory),
            file_index_64=file_index,
            compressed_size=(
                int(compression.CompressedFileSize) if compression is not None else None
            ),
            compression_format=(
                int(compression.CompressionFormat) if compression is not None else None
            ),
            compression_unit_shift=(
                int(compression.CompressionUnitShift) if compression is not None else None
            ),
            chunk_shift=int(compression.ChunkShift) if compression is not None else None,
            cluster_shift=(int(compression.ClusterShift) if compression is not None else None),
            storage=storage_doc,
            usn=usn,
        )

    def read_usn(self, fd: int) -> Mapping[str, Any] | None:
        handle = self._handle_from_fd(fd)
        try:
            raw, _ = self._device_io(
                handle,
                FSCTL_READ_FILE_USN_DATA,
                output_size=64 * 1024,
            )
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            if _is_unsupported_winerror(error) or error in {
                ERROR_ACCESS_DENIED,
                ERROR_FILE_NOT_FOUND,
            }:
                return None
            raise
        if len(raw) < 8:
            return {"raw": raw, "parse_status": "unresolved"}
        record_length, major, minor = struct.unpack_from("<IHH", raw, 0)
        if record_length < 8 or record_length > len(raw):
            return {"raw": raw, "parse_status": "unresolved"}
        doc: dict[str, Any] = {
            "raw": raw[:record_length],
            "record_length": record_length,
            "major_version": major,
            "minor_version": minor,
            "parse_status": "parsed",
        }
        try:
            if major == 2 and record_length >= 60:
                doc.update(
                    {
                        "file_reference_number": f"{struct.unpack_from('<Q', raw, 8)[0]:016x}",
                        "parent_file_reference_number": (
                            f"{struct.unpack_from('<Q', raw, 16)[0]:016x}"
                        ),
                        "usn": str(struct.unpack_from("<q", raw, 24)[0]),
                        "security_id": int(struct.unpack_from("<I", raw, 48)[0]),
                        "file_attributes": int(struct.unpack_from("<I", raw, 52)[0]),
                    }
                )
                name_len, name_off = struct.unpack_from("<HH", raw, 56)
            elif major == 3 and record_length >= 76:
                doc.update(
                    {
                        "file_reference_number": raw[8:24].hex(),
                        "parent_file_reference_number": raw[24:40].hex(),
                        "usn": str(struct.unpack_from("<q", raw, 40)[0]),
                        "security_id": int(struct.unpack_from("<I", raw, 64)[0]),
                        "file_attributes": int(struct.unpack_from("<I", raw, 68)[0]),
                    }
                )
                name_len, name_off = struct.unpack_from("<HH", raw, 72)
            else:
                doc["parse_status"] = "unsupported_version"
                return doc
            if name_len and name_off + name_len <= record_length and name_len % 2 == 0:
                name_raw = raw[name_off : name_off + name_len]
                doc["file_name"] = name_raw.decode("utf-16le", "surrogatepass")
                doc["file_name_utf16le"] = name_raw
        except (IndexError, struct.error, UnicodeError):
            doc["parse_status"] = "unresolved"
        # FSCTL_READ_FILE_USN_DATA documentation explicitly says TimeStamp,
        # Reason, and SourceInfo in this returned record are invalid.  They are
        # deliberately not asserted, although their bytes remain in raw evidence.
        return doc

    def object_id(self, fd: int) -> bytes | None:
        handle = self._handle_from_fd(fd)
        try:
            raw, _ = self._device_io(
                handle,
                FSCTL_GET_OBJECT_ID,
                output_size=64,
            )
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            if _is_unsupported_winerror(error) or error in {
                ERROR_FILE_NOT_FOUND,
                ERROR_OBJECT_NOT_FOUND,
            }:
                return None
            raise
        return raw or None

    def integrity_info(self, fd: int) -> IntegrityInfo | None:
        handle = self._handle_from_fd(fd)
        try:
            raw, _ = self._device_io(
                handle,
                FSCTL_GET_INTEGRITY_INFORMATION,
                output_size=ctypes.sizeof(_FSCTL_GET_INTEGRITY_INFORMATION_BUFFER),
            )
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            if _is_unsupported_winerror(error):
                return None
            raise
        if len(raw) < ctypes.sizeof(_FSCTL_GET_INTEGRITY_INFORMATION_BUFFER):
            raise NativeObservationError("short integrity-information response")
        info = _FSCTL_GET_INTEGRITY_INFORMATION_BUFFER.from_buffer_copy(raw)
        return IntegrityInfo(
            checksum_algorithm=int(info.ChecksumAlgorithm),
            flags=int(info.Flags),
            checksum_chunk_size=int(info.ChecksumChunkSizeInBytes),
            cluster_size=int(info.ClusterSizeInBytes),
        )

    def allocated_ranges(
        self, fd: int, size: int, *, maximum_extents: int
    ) -> tuple[list[tuple[int, int]], bool] | None:
        if size == 0:
            return [], True
        handle = self._handle_from_fd(fd)
        ranges: list[tuple[int, int]] = []
        position = 0
        complete = True
        item_size = ctypes.sizeof(_FILE_ALLOCATED_RANGE_BUFFER)
        capacity = min(maximum_extents, 4096)
        while position < size:
            query = struct.pack("<qq", position, size - position)
            try:
                raw, error = self._device_io(
                    handle,
                    FSCTL_QUERY_ALLOCATED_RANGES,
                    input_bytes=query,
                    output_size=max(item_size, capacity * item_size),
                    allow_more_data=True,
                )
            except OSError as exc:
                code = int(getattr(exc, "winerror", exc.errno or 0))
                if _is_unsupported_winerror(code):
                    return None
                raise
            if len(raw) % item_size:
                raise NativeObservationError("misaligned allocated-range response")
            batch: list[tuple[int, int]] = []
            for offset in range(0, len(raw), item_size):
                item = _FILE_ALLOCATED_RANGE_BUFFER.from_buffer_copy(
                    raw[offset : offset + item_size]
                )
                start = int(item.FileOffset)
                length = int(item.Length)
                if length < 0 or start < 0:
                    raise NativeObservationError("invalid allocated range")
                batch.append((start, length))
            ranges.extend(batch)
            if len(ranges) >= maximum_extents:
                ranges = ranges[:maximum_extents]
                complete = False
                break
            if error != ERROR_MORE_DATA:
                break
            if not batch:
                complete = False
                break
            next_position = batch[-1][0] + batch[-1][1]
            if next_position <= position:
                complete = False
                break
            position = next_position
        return ranges, complete

    def _backup_read_chunk(self, handle: int, count: int, context: ctypes.c_void_p) -> bytes:
        if count <= 0:
            return b""
        buffer = (_BYTE * count)()
        read = _DWORD()
        ctypes.set_last_error(0)
        ok = self.kernel32.BackupRead(
            wintypes.HANDLE(handle),
            buffer,
            count,
            ctypes.byref(read),
            False,
            False,
            ctypes.byref(context),
        )
        if not ok:
            raise _winerror(ctypes.get_last_error(), "BackupRead failed")
        return bytes(buffer[: int(read.value)])

    def _backup_read_exact(
        self,
        handle: int,
        count: int,
        context: ctypes.c_void_p,
        *,
        allow_eof: bool = False,
    ) -> bytes:
        parts: list[bytes] = []
        remaining = count
        while remaining:
            chunk = self._backup_read_chunk(handle, remaining, context)
            if not chunk:
                if allow_eof and remaining == count:
                    return b""
                raise NativeObservationError("unexpected EOF in BackupRead stream")
            parts.append(chunk)
            remaining -= len(chunk)
        return b"".join(parts)

    def _backup_skip(
        self,
        handle: int,
        count: int,
        context: ctypes.c_void_p,
        *,
        chunk_bytes: int,
    ) -> None:
        if count <= 0:
            return
        low = _DWORD(count & 0xFFFFFFFF)
        high = _DWORD((count >> 32) & 0xFFFFFFFF)
        low_done = _DWORD()
        high_done = _DWORD()
        ctypes.set_last_error(0)
        ok = self.kernel32.BackupSeek(
            wintypes.HANDLE(handle),
            low,
            high,
            ctypes.byref(low_done),
            ctypes.byref(high_done),
            ctypes.byref(context),
        )
        sought = (int(high_done.value) << 32) | int(low_done.value)
        if ok and sought == count:
            return
        remaining = count - sought
        while remaining > 0:
            chunk = self._backup_read_chunk(handle, min(remaining, chunk_bytes), context)
            if not chunk:
                raise NativeObservationError("BackupRead could not skip stream data")
            remaining -= len(chunk)

    def backup_streams(self, fd: int, request: ObservationRequest) -> list[BackupStreamCapture]:
        handle = self._handle_from_fd(fd)
        context = ctypes.c_void_p()
        streams: list[BackupStreamCapture] = []
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            while True:
                header = self._backup_read_exact(
                    handle, _BACKUP_HEADER.size, context, allow_eof=True
                )
                if not header:
                    break
                stream_id, attributes, size, name_size = _BACKUP_HEADER.unpack(header)
                if size < 0 or name_size % 2:
                    raise NativeObservationError("invalid WIN32_STREAM_ID header")
                name_raw = self._backup_read_exact(handle, name_size, context)
                name = name_raw.decode("utf-16le", "surrogatepass") if name_raw else ""
                if stream_id in {BACKUP_DATA, BACKUP_SECURITY_DATA}:
                    self._backup_skip(
                        handle,
                        size,
                        context,
                        chunk_bytes=request.policy.native_stream_chunk_bytes,
                    )
                    continue
                if len(streams) >= request.policy.maximum_native_streams:
                    raise NativeObservationError(
                        "BackupRead stream count exceeded maximum_native_streams"
                    )

                status = "captured"
                data: bytes | None = None
                sha256: str | None = None
                note: str | None = None
                if size <= request.policy.inline_native_value_bytes:
                    data = self._backup_read_exact(handle, size, context)
                    sha256 = hashlib.sha256(data).hexdigest()
                elif size > request.policy.maximum_native_value_bytes:
                    if request.policy.large_value_disposition is LargeValueDisposition.FAIL:
                        raise NativeObservationError(
                            f"native backup stream size {size} exceeds policy maximum"
                        )
                    status = "not_retained"
                    note = "Stream exceeded maximum_native_value_bytes and was not retained."
                    self._backup_skip(
                        handle,
                        size,
                        context,
                        chunk_bytes=request.policy.native_stream_chunk_bytes,
                    )
                elif request.policy.large_value_disposition is LargeValueDisposition.DIGEST_ONLY:
                    status = "digest_only"
                    digest = hashlib.sha256()
                    remaining = size
                    while remaining:
                        chunk = self._backup_read_exact(
                            handle,
                            min(remaining, request.policy.native_stream_chunk_bytes),
                            context,
                        )
                        digest.update(chunk)
                        remaining -= len(chunk)
                    sha256 = digest.hexdigest()
                elif request.policy.large_value_disposition is LargeValueDisposition.NOT_RETAINED:
                    status = "not_retained"
                    note = "Stream exceeded inline_native_value_bytes and policy forbids retention."
                    self._backup_skip(
                        handle,
                        size,
                        context,
                        chunk_bytes=request.policy.native_stream_chunk_bytes,
                    )
                else:  # pragma: no cover - enum exhaustiveness
                    raise NativeObservationError("unsupported large-value disposition")
                streams.append(
                    BackupStreamCapture(
                        stream_id=stream_id,
                        attributes=attributes,
                        name=name,
                        name_bytes=name_raw,
                        size=size,
                        capture_status=status,
                        data=data,
                        sha256=sha256,
                        note=note,
                    )
                )
        finally:
            read = _DWORD()
            # Abort releases the opaque BackupRead context. It is required even
            # after normal completion.
            self.kernel32.BackupRead(
                wintypes.HANDLE(handle),
                None,
                0,
                ctypes.byref(read),
                True,
                False,
                ctypes.byref(context),
            )
            os.lseek(fd, 0, os.SEEK_SET)
        return streams

    def _sid_to_string(self, sid: int) -> str:
        text = wintypes.LPWSTR()
        ctypes.set_last_error(0)
        if not self.advapi32.ConvertSidToStringSidW(wintypes.LPVOID(sid), ctypes.byref(text)):
            raise _winerror(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return str(text.value)
        finally:
            self.kernel32.LocalFree(ctypes.cast(text, wintypes.LPVOID))

    def _lookup_sid(self, sid: int) -> str | None:
        name_len = _DWORD()
        domain_len = _DWORD()
        sid_use = _DWORD()
        ctypes.set_last_error(0)
        self.advapi32.LookupAccountSidW(
            None,
            wintypes.LPVOID(sid),
            None,
            ctypes.byref(name_len),
            None,
            ctypes.byref(domain_len),
            ctypes.byref(sid_use),
        )
        error = ctypes.get_last_error()
        if error not in {ERROR_INSUFFICIENT_BUFFER, ERROR_NONE_MAPPED}:
            raise _winerror(error, "LookupAccountSidW sizing failed")
        if error == ERROR_NONE_MAPPED or not name_len.value:
            return None
        name = ctypes.create_unicode_buffer(int(name_len.value))
        domain = ctypes.create_unicode_buffer(max(1, int(domain_len.value)))
        ctypes.set_last_error(0)
        if not self.advapi32.LookupAccountSidW(
            None,
            wintypes.LPVOID(sid),
            name,
            ctypes.byref(name_len),
            domain,
            ctypes.byref(domain_len),
            ctypes.byref(sid_use),
        ):
            error = ctypes.get_last_error()
            if error == ERROR_NONE_MAPPED:
                return None
            raise _winerror(error, "LookupAccountSidW failed")
        return f"{domain.value}\\{name.value}" if domain.value else name.value

    def _principal_from_sid(self, sid: int | None, *, resolve: bool) -> WindowsPrincipal | None:
        if not sid:
            return None
        sid_text = self._sid_to_string(sid)
        name = self._lookup_sid(sid) if resolve else None
        return WindowsPrincipal(sid=sid_text, name=name)

    def security_descriptor(
        self,
        fd: int,
        *,
        include_sacl: bool,
        resolve_principals: bool,
    ) -> SecurityDescriptorCapture:
        handle = self._handle_from_fd(fd)
        requested = (
            OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        )
        if include_sacl:
            requested |= SACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION
        needed = _DWORD()
        ctypes.set_last_error(0)
        self.advapi32.GetKernelObjectSecurity(
            wintypes.HANDLE(handle), requested, None, 0, ctypes.byref(needed)
        )
        error = ctypes.get_last_error()
        if error != ERROR_INSUFFICIENT_BUFFER or not needed.value:
            raise _winerror(error, "GetKernelObjectSecurity sizing failed")
        buffer = ctypes.create_string_buffer(int(needed.value))
        ctypes.set_last_error(0)
        if not self.advapi32.GetKernelObjectSecurity(
            wintypes.HANDLE(handle),
            requested,
            ctypes.byref(buffer),
            int(needed.value),
            ctypes.byref(needed),
        ):
            raise _winerror(ctypes.get_last_error(), "GetKernelObjectSecurity failed")
        base = ctypes.addressof(buffer)
        owner_ptr = wintypes.LPVOID()
        group_ptr = wintypes.LPVOID()
        defaulted = _BOOL()
        owner: WindowsPrincipal | None = None
        group: WindowsPrincipal | None = None
        if self.advapi32.GetSecurityDescriptorOwner(
            wintypes.LPVOID(base), ctypes.byref(owner_ptr), ctypes.byref(defaulted)
        ):
            owner = self._principal_from_sid(
                ctypes.cast(owner_ptr, ctypes.c_void_p).value,
                resolve=resolve_principals,
            )
        if self.advapi32.GetSecurityDescriptorGroup(
            wintypes.LPVOID(base), ctypes.byref(group_ptr), ctypes.byref(defaulted)
        ):
            group = self._principal_from_sid(
                ctypes.cast(group_ptr, ctypes.c_void_p).value,
                resolve=resolve_principals,
            )
        control_word = _WORD()
        revision = _DWORD()
        control: int | None = None
        if self.advapi32.GetSecurityDescriptorControl(
            wintypes.LPVOID(base), ctypes.byref(control_word), ctypes.byref(revision)
        ):
            control = int(control_word.value)
        sddl_ptr = wintypes.LPWSTR()
        sddl_len = _DWORD()
        sddl: str | None = None
        if self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            wintypes.LPVOID(base),
            SDDL_REVISION_1,
            requested,
            ctypes.byref(sddl_ptr),
            ctypes.byref(sddl_len),
        ):
            try:
                sddl = str(sddl_ptr.value)
            finally:
                self.kernel32.LocalFree(ctypes.cast(sddl_ptr, wintypes.LPVOID))
        return SecurityDescriptorCapture(
            raw=bytes(buffer.raw[: int(needed.value)]),
            security_information=requested,
            owner=owner,
            group=group,
            sddl=sddl,
            control=control,
            sacl_included=include_sacl,
        )

    def current_token(self, *, resolve: bool) -> tuple[WindowsPrincipal | None, str]:
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            return None, "unknown"
        try:
            needed = _DWORD()
            self.advapi32.GetTokenInformation(
                token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed)
            )
            if not needed.value:
                return None, "unknown"
            buffer = ctypes.create_string_buffer(int(needed.value))
            if not self.advapi32.GetTokenInformation(
                token,
                TOKEN_USER_CLASS,
                ctypes.byref(buffer),
                int(needed.value),
                ctypes.byref(needed),
            ):
                return None, "unknown"
            token_user = _TOKEN_USER.from_buffer_copy(buffer.raw[: ctypes.sizeof(_TOKEN_USER)])
            principal = self._principal_from_sid(
                ctypes.cast(token_user.User.Sid, ctypes.c_void_p).value,
                resolve=resolve,
            )
            elevation = _TOKEN_ELEVATION()
            returned = _DWORD()
            elevated = False
            if self.advapi32.GetTokenInformation(
                token,
                TOKEN_ELEVATION_CLASS,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            ):
                elevated = bool(elevation.TokenIsElevated)
            if principal and principal.sid == "S-1-5-18":
                privilege = "system"
            else:
                privilege = "elevated" if elevated else "unprivileged"
            return principal, privilege
        finally:
            self.kernel32.CloseHandle(token)

    def os_information(self) -> Mapping[str, str]:
        values: dict[str, str] = {}
        try:
            import winreg

            path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                for name in (
                    "ProductName",
                    "DisplayVersion",
                    "CurrentBuildNumber",
                    "UBR",
                    "EditionID",
                    "InstallationType",
                ):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    values[name] = str(value)
        except (ImportError, OSError):
            pass
        version = sys.getwindowsversion()
        build = values.get("CurrentBuildNumber") or str(version.build)
        if values.get("UBR"):
            build = f"{build}.{values['UBR']}"
        display = values.get("DisplayVersion") or f"build {version.build}"
        product = values.get("ProductName") or "Microsoft Windows"
        return {
            "name": product,
            "version": display,
            "build": f"{version.major}.{version.minor}.{build}",
            "edition": values.get("EditionID", "unknown"),
            "installation_type": values.get("InstallationType", "unknown"),
            "kernel_release": f"{version.major}.{version.minor}.{version.build}",
            "kernel_version": platform.version() or str(version),
        }

    def _final_path(self, handle: int) -> str | None:
        ctypes.set_last_error(0)
        needed = int(self.kernel32.GetFinalPathNameByHandleW(wintypes.HANDLE(handle), None, 0, 0))
        if not needed:
            return None
        buffer = ctypes.create_unicode_buffer(needed + 1)
        written = int(
            self.kernel32.GetFinalPathNameByHandleW(wintypes.HANDLE(handle), buffer, len(buffer), 0)
        )
        if not written or written >= len(buffer):
            return None
        return buffer.value

    def volume_info(self, fd: int, path: str) -> WindowsVolumeInfo:
        handle = self._handle_from_fd(fd)
        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = _DWORD()
        maximum_component = _DWORD()
        flags = _DWORD()
        if not self.kernel32.GetVolumeInformationByHandleW(
            wintypes.HANDLE(handle),
            label,
            len(label),
            ctypes.byref(serial),
            ctypes.byref(maximum_component),
            ctypes.byref(flags),
            filesystem,
            len(filesystem),
        ):
            raise _winerror(ctypes.get_last_error(), "GetVolumeInformationByHandleW failed")
        final_path = self._final_path(handle)
        mount_buffer = ctypes.create_unicode_buffer(32768)
        mount_path: str | None = None
        # Some redirectors and older filesystem providers reject the extended
        # ``\\?\`` spelling returned by GetFinalPathNameByHandleW even though
        # they accept the caller's DOS path. Prefer the handle-derived spelling,
        # then retry the original source locator without changing the observed
        # file identity.
        for path_for_volume in dict.fromkeys(
            candidate for candidate in (final_path, path) if candidate
        ):
            mount_buffer.value = ""
            if self.kernel32.GetVolumePathNameW(path_for_volume, mount_buffer, len(mount_buffer)):
                mount_path = mount_buffer.value
                break
        volume_guid_path: str | None = None
        drive_type: int | None = None
        sectors_per_cluster: int | None = None
        bytes_per_sector: int | None = None
        if mount_path:
            guid_buffer = ctypes.create_unicode_buffer(128)
            if self.kernel32.GetVolumeNameForVolumeMountPointW(
                mount_path, guid_buffer, len(guid_buffer)
            ):
                volume_guid_path = guid_buffer.value
            drive_type = int(self.kernel32.GetDriveTypeW(mount_path))
            spc = _DWORD()
            bps = _DWORD()
            free_clusters = _DWORD()
            total_clusters = _DWORD()
            if self.kernel32.GetDiskFreeSpaceW(
                mount_path,
                ctypes.byref(spc),
                ctypes.byref(bps),
                ctypes.byref(free_clusters),
                ctypes.byref(total_clusters),
            ):
                sectors_per_cluster = int(spc.value)
                bytes_per_sector = int(bps.value)
        return WindowsVolumeInfo(
            filesystem_name=filesystem.value or "unknown",
            volume_label=label.value,
            volume_serial_number=int(serial.value),
            maximum_component_length=int(maximum_component.value),
            filesystem_flags=int(flags.value),
            mount_path=mount_path,
            volume_guid_path=volume_guid_path,
            drive_type=drive_type,
            sectors_per_cluster=sectors_per_cluster,
            bytes_per_sector=bytes_per_sector,
            final_path=final_path,
        )


class WindowsBackend(PlatformBackend):
    """Riverhog provenance mapping for Windows 11 and compatible Windows systems."""

    platform_family = PLATFORM_FAMILY

    def __init__(
        self,
        *,
        native: WindowsNativeAPI | Any | None = None,
        enforce_platform: bool = True,
    ) -> None:
        self.native = native
        self.enforce_platform = enforce_platform
        self._opened: dict[int, OpenedWindowsFile] = {}

    def assert_supported(self) -> None:
        if self.enforce_platform and sys.platform != "win32":
            raise UnsupportedPlatformError("Windows observer requires Windows")
        if self.native is None:
            self.native = WindowsNativeAPI()

    @property
    def api(self) -> Any:
        if self.native is None:
            raise RuntimeError("Windows native API is unavailable before platform validation")
        return self.native

    def absolute_path(self, path: PathInput) -> str:
        raw = os.fspath(path)
        if not isinstance(raw, str):
            raise TypeError(
                "Windows observer requires a Unicode path; bytes paths are not portable"
            )
        return os.path.abspath(raw)

    def preflight_path(self, path: str | bytes) -> None:
        if not isinstance(path, str):
            raise TypeError("Windows observer requires a Unicode path")
        info = self.api.inspect_path(path, follow_non_name_surrogate=True)
        if info.directory:
            raise UnsupportedFileTypeError("target is not a regular file")
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT and _is_name_surrogate(info.reparse_tag):
            raise SymlinkRefusedError("refusing to observe a name-surrogate reparse point")

    def path_matches(self, path: str | bytes, stat: NativeStat) -> bool:
        if not isinstance(path, str):
            return False
        try:
            info = self.api.inspect_path(path, follow_non_name_surrogate=True)
        except OSError:
            return False
        if info.directory or _is_name_surrogate(info.reparse_tag):
            return False
        expected = (
            int(stat.extras.get("volume_serial_number", -1)),
            str(stat.extras.get("file_id_hex", "")),
            str(stat.extras.get("file_id_scheme", "")),
        )
        actual = (
            info.identity.volume_serial_number,
            info.identity.file_id_hex,
            info.identity.scheme,
        )
        return actual == expected

    def locator(
        self,
        path: str | bytes,
        *,
        kind: str,
        authority_id: str | None = None,
    ) -> JsonObject:
        if not isinstance(path, str):
            raise TypeError("Windows locator requires Unicode path")
        return windows_locator(path, kind=kind, authority_id=authority_id)

    def path_basename(self, path: str | bytes) -> str:
        if not isinstance(path, str):
            raise TypeError("Windows path must be Unicode")
        import ntpath

        return ntpath.basename(path.rstrip("\\/"))

    def path_is_absolute(self, path: str | bytes) -> bool:
        if not isinstance(path, str):
            return False
        import ntpath

        drive, tail = ntpath.splitdrive(path)
        # Reject drive-relative forms such as C:payload.dat as well as absolute
        # archive payload locators. Riverhog provenance bindings use portable relative paths.
        return bool(drive) or ntpath.isabs(tail)

    def open_readonly(
        self, path: str | bytes, request: ObservationRequest
    ) -> tuple[int, list[JsonObject], bool]:
        if not isinstance(path, str):
            raise TypeError("Windows observer requires a Unicode path")
        opened, diagnostics = self.api.open_regular_file(path, request)
        self._opened[opened.fd] = opened
        return opened.fd, diagnostics, False

    def release_fd(self, fd: int) -> None:
        self._opened.pop(fd, None)

    def stat_fd(self, fd: int) -> NativeStat:
        opened = self._opened.get(fd)
        capture_usn = opened.capture_usn if opened else True
        snapshot = self.api.snapshot(fd, capture_usn=capture_usn)
        writable = not bool(snapshot.attributes & 0x00000001)
        permissions = 0o666 if writable else 0o444
        extras: dict[str, Any] = {
            "volume_serial_number": snapshot.identity.volume_serial_number,
            "file_id_hex": snapshot.identity.file_id_hex,
            "file_id_bits": snapshot.identity.file_id_bits,
            "file_id_scheme": snapshot.identity.scheme,
            "file_id_source_api": snapshot.identity.source_api,
            "file_id_source_field": snapshot.identity.source_field,
            "creation_ticks": snapshot.creation_ticks,
            "access_ticks": snapshot.access_ticks,
            "write_ticks": snapshot.write_ticks,
            "change_ticks": snapshot.change_ticks,
            "file_attributes": snapshot.attributes,
            "reparse_tag": snapshot.reparse_tag,
            "allocation_size": snapshot.allocation_size,
            "delete_pending": snapshot.delete_pending,
            "file_index_64": snapshot.file_index_64,
            "compressed_size": snapshot.compressed_size,
            "compression_format": snapshot.compression_format,
            "compression_unit_shift": snapshot.compression_unit_shift,
            "chunk_shift": snapshot.chunk_shift,
            "cluster_shift": snapshot.cluster_shift,
            "storage": snapshot.storage,
            "usn": snapshot.usn,
            "usn_value": (snapshot.usn or {}).get("usn") if snapshot.usn else None,
        }
        return NativeStat(
            device=snapshot.identity.volume_serial_number,
            inode=int.from_bytes(snapshot.identity.file_id, "big", signed=False),
            mode=statmod.S_IFREG | permissions,
            nlink=snapshot.number_of_links,
            uid=0,
            gid=0,
            size=snapshot.end_of_file,
            atime_ns=(
                _filetime_to_unix_ns(snapshot.access_ticks) if snapshot.access_ticks > 0 else 0
            ),
            mtime_ns=(
                _filetime_to_unix_ns(snapshot.write_ticks) if snapshot.write_ticks > 0 else 0
            ),
            ctime_ns=(
                _filetime_to_unix_ns(snapshot.change_ticks) if snapshot.change_ticks > 0 else 0
            ),
            birthtime_ns=(
                _filetime_to_unix_ns(snapshot.creation_ticks)
                if snapshot.creation_ticks > 0
                else None
            ),
            flags=snapshot.attributes,
            extras=extras,
        )

    def stability_differences(self, before: NativeStat, after: NativeStat) -> list[str]:
        fields = (
            "volume_serial_number",
            "file_id_hex",
            "file_id_scheme",
            "write_ticks",
            "change_ticks",
            "file_attributes",
            "reparse_tag",
            "allocation_size",
            "delete_pending",
            "usn_value",
        )
        differences = [
            field for field in fields if before.extras.get(field) != after.extras.get(field)
        ]
        if before.size != after.size:
            differences.append("size")
        if before.nlink != after.nlink:
            differences.append("number_of_links")
        return differences

    def finalize_timestamps(
        self,
        collection: NativeCollection,
        final_stat: NativeStat,
        request: ObservationRequest,
    ) -> None:
        if not request.policy.include_access_time:
            return
        ticks = int(final_stat.extras.get("access_ticks", 0))
        replacement = _filetime_observation("accessed", ticks, "FileBasicInfo.LastAccessTime")
        for index, timestamp in enumerate(collection.timestamps):
            if timestamp.get("kind") == "accessed":
                collection.timestamps[index] = replacement
                return

    def collect(
        self,
        fd: int,
        path: str | bytes,
        stat: NativeStat,
        request: ObservationRequest,
    ) -> NativeCollection:
        if not isinstance(path, str):
            raise TypeError("Windows observer requires a Unicode path")
        result = NativeCollection()
        result.timestamps = self._timestamps(stat, request)
        result.coverage.update(
            {
                "timestamps": "complete",
                "native_identifiers": "complete",
                "resource_forks": "not_applicable",
            }
        )
        volume = self.api.volume_info(fd, path)
        volume_authority = self._volume_authority(request.host_id, stat, volume)
        result.native_identifiers = self._native_identifiers(
            stat, request.host_id, volume_authority
        )

        if request.policy.capture_acl:
            self._capture_security(fd, request, result)
        else:
            result.coverage.update(
                {
                    "ownership": "not_requested",
                    "permissions": "not_requested",
                    "access_control": "not_requested",
                    "security_metadata": "not_requested",
                }
            )

        if request.policy.capture_xattrs or request.policy.capture_special_features:
            self._capture_backup_streams(fd, request, result)
        else:
            result.coverage["extended_attributes"] = "not_requested"
            result.coverage["alternate_streams"] = "not_requested"

        if request.policy.capture_file_flags:
            self._capture_file_flags(stat, request, result)
        else:
            result.coverage["file_flags"] = "not_requested"

        if request.policy.capture_sparse_map:
            self._capture_sparse_map(fd, stat, request, result)
        else:
            result.coverage["storage_layout"] = "not_requested"

        if request.policy.capture_special_features:
            self._capture_special_features(fd, stat, request, result)
        else:
            result.coverage["special_file_features"] = "not_requested"

        if request.policy.capture_native_stat:
            self._capture_native_stat(stat, request, result)
            result.coverage["native_metadata_other"] = "complete"
        else:
            result.coverage["native_metadata_other"] = "not_requested"

        result.environment = self._environment(stat, path, volume, request)
        result.extension_drafts.append(
            ExtensionDraft(
                subject_role="environment",
                property="https://nashspence.github.io/riverhog/v1/provenance/observers/vocab/windows-volume-context",
                value=_schema_value(
                    "windows-volume-context",
                    self._volume_context(volume),
                ),
                note=("Handle-bound Windows volume and filesystem context for the observed file."),
            )
        )
        return result

    @staticmethod
    def _timestamps(stat: NativeStat, request: ObservationRequest) -> list[JsonObject]:
        values = [
            _filetime_observation(
                "created",
                int(stat.extras.get("creation_ticks", 0)),
                "FileBasicInfo.CreationTime",
            ),
            _filetime_observation(
                "content_modified",
                int(stat.extras.get("write_ticks", 0)),
                "FileBasicInfo.LastWriteTime",
            ),
            _filetime_observation(
                "metadata_changed",
                int(stat.extras.get("change_ticks", 0)),
                "FileBasicInfo.ChangeTime",
            ),
        ]
        if request.policy.include_access_time:
            values.append(
                _filetime_observation(
                    "accessed",
                    int(stat.extras.get("access_ticks", 0)),
                    "FileBasicInfo.LastAccessTime",
                )
            )
        return values

    @staticmethod
    def _volume_authority(host_id: str, stat: NativeStat, volume: WindowsVolumeInfo) -> str:
        if volume.volume_guid_path:
            key = f"windows-volume:{host_id}:guid:{volume.volume_guid_path.lower()}"
        else:
            key = (
                f"windows-volume:{host_id}:serial:"
                f"{stat.extras.get('volume_serial_number', volume.volume_serial_number)}:"
                f"{volume.mount_path or ''}"
            )
        return f"urn:uuid:{uuid.uuid5(OBSERVER_NAMESPACE, key)}"

    @staticmethod
    def _native_identifiers(
        stat: NativeStat, host_id: str, volume_authority: str
    ) -> list[JsonObject]:
        scheme = str(stat.extras["file_id_scheme"])
        if scheme == "windows-file-index-64":
            value = str(int.from_bytes(bytes.fromhex(str(stat.extras["file_id_hex"])), "big"))
        else:
            value = str(stat.extras["file_id_hex"])
        rows = [
            observed_identifier(
                scheme=scheme,
                value=value,
                scope="volume",
                authority_id=volume_authority,
                platform="windows",
                api=str(stat.extras["file_id_source_api"]),
                field=str(stat.extras["file_id_source_field"]),
            ),
            observed_identifier(
                scheme="windows-file-id-volume-serial-64",
                value=f"{int(stat.extras['volume_serial_number']):016x}",
                scope="host",
                authority_id=host_id,
                platform="windows",
                api="GetFileInformationByHandleEx",
                field="FileIdInfo.VolumeSerialNumber",
            ),
        ]
        if scheme != "windows-file-index-64":
            rows.append(
                observed_identifier(
                    scheme="windows-file-index-64",
                    value=str(int(stat.extras.get("file_index_64", 0))),
                    scope="volume",
                    authority_id=volume_authority,
                    platform="windows",
                    api="GetFileInformationByHandle",
                    field="nFileIndexHigh:nFileIndexLow",
                )
            )
        return rows

    def _capture_security(
        self,
        fd: int,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        include_sacl = request.policy.capture_system_acl
        try:
            capture = self.api.security_descriptor(
                fd,
                include_sacl=include_sacl,
                resolve_principals=request.policy.resolve_principals,
            )
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            if include_sacl and error in {ERROR_ACCESS_DENIED, ERROR_PRIVILEGE_NOT_HELD}:
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="access_control",
                        code="windows_sacl_capture_unavailable",
                        message=(
                            "SACL capture failed; owner, group, and DACL capture was retried."
                        ),
                        native_code=str(error),
                        source_descriptor=source(
                            "windows", "GetKernelObjectSecurity", "SACL_SECURITY_INFORMATION"
                        ),
                    )
                )
                try:
                    capture = self.api.security_descriptor(
                        fd,
                        include_sacl=False,
                        resolve_principals=request.policy.resolve_principals,
                    )
                except OSError as fallback:
                    self._security_failure(fallback, result)
                    return
                merge_coverage(result.coverage, "access_control", "partial")
                merge_coverage(result.coverage, "security_metadata", "partial")
            else:
                self._security_failure(exc, result)
                return

        status, value, note = retained_native_value(
            capture.raw,
            agent_id=request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
            request=request,
        )
        structured = {
            "security_information": capture.security_information,
            "control": capture.control if capture.control is not None else 0,
            "owner_sid": capture.owner.sid if capture.owner else "",
            "group_sid": capture.group.sid if capture.group else "",
            "sacl_included": capture.sacl_included,
        }
        interpretations: list[JsonObject] = [
            {
                "kind": "structured_parse",
                "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-security-descriptor.json",
                "value": _schema_value("windows-security-descriptor", structured),
                "agent_id": request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                "confidence": "high",
            }
        ]
        if capture.sddl:
            interpretations.append(
                {
                    "kind": "text_decode",
                    "value": {
                        "type": "text",
                        "data": safe_portable_text(capture.sddl),
                        "source_encoding": "UTF-16",
                    },
                    "agent_id": request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                    "confidence": "high",
                    "note": "SDDL interpretation generated by Windows Advapi32.",
                }
            )
        result.native_metadata.append(
            _native_row(
                kind="security_descriptor",
                category="access_control",
                name="self-relative-security-descriptor",
                api="GetKernelObjectSecurity",
                field="SECURITY_DESCRIPTOR_RELATIVE",
                value=value,
                status=status,
                sensitivity="security_sensitive",
                note=note,
                interpretations=interpretations,
                observed_byte_length=len(capture.raw),
            )
        )
        access: JsonObject = {}
        owner = _principal_document(capture.owner, "user")
        group = _principal_document(capture.group, "group")
        if owner:
            access["owner"] = owner
        if group:
            access["group"] = group
        result.access = access or None
        result.coverage.setdefault("access_control", "complete")
        result.coverage.setdefault("security_metadata", "complete")
        result.coverage["permissions"] = "complete" if capture.raw else "partial"
        result.coverage["ownership"] = "complete" if capture.owner is not None else "partial"

    @staticmethod
    def _security_failure(exc: OSError, result: NativeCollection) -> None:
        error = int(getattr(exc, "winerror", exc.errno or 0))
        result.coverage.update(
            {
                "ownership": "partial",
                "permissions": "partial",
                "access_control": "partial",
                "security_metadata": "partial",
            }
        )
        result.diagnostics.append(
            diagnostic(
                severity="error",
                category="access_control",
                code="windows_security_descriptor_unavailable",
                message="The file security descriptor could not be captured.",
                native_code=str(error),
                source_descriptor=source(
                    "windows", "GetKernelObjectSecurity", "SECURITY_DESCRIPTOR_RELATIVE"
                ),
            )
        )

    @staticmethod
    def _stream_value(
        capture: BackupStreamCapture,
        *,
        agent_id: str,
    ) -> JsonObject | None:
        if capture.capture_status == "captured" and capture.data is not None:
            return bytes_value(capture.data, agent_id=agent_id)
        if capture.capture_status == "digest_only" and capture.sha256:
            return {
                "type": "digest",
                "byte_length": capture.size,
                "digests": [
                    digest_assertion(
                        capture.sha256,
                        agent_id=agent_id,
                        purpose="native_metadata",
                    )
                ],
            }
        return None

    def _capture_backup_streams(
        self,
        fd: int,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        try:
            streams = self.api.backup_streams(fd, request)
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            status = "not_supported" if _is_unsupported_winerror(error) else "partial"
            if request.policy.capture_xattrs:
                result.coverage["extended_attributes"] = status
            if request.policy.capture_special_features:
                result.coverage["alternate_streams"] = status
            if status == "partial":
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="alternate_streams",
                        code="windows_backup_stream_enumeration_failed",
                        message="BackupRead metadata stream enumeration failed.",
                        native_code=str(error),
                        source_descriptor=source("windows", "BackupRead", "WIN32_STREAM_ID"),
                    )
                )
            return
        except NativeObservationError as exc:
            if request.policy.capture_xattrs:
                result.coverage["extended_attributes"] = "partial"
            if request.policy.capture_special_features:
                result.coverage["alternate_streams"] = "partial"
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="alternate_streams",
                    code="windows_backup_stream_invalid",
                    message=str(exc),
                    source_descriptor=source("windows", "BackupRead", "WIN32_STREAM_ID"),
                )
            )
            return

        if request.policy.capture_xattrs:
            result.coverage["extended_attributes"] = "complete"
        else:
            result.coverage["extended_attributes"] = "not_requested"
        if request.policy.capture_special_features:
            result.coverage["alternate_streams"] = "complete"
        else:
            result.coverage["alternate_streams"] = "not_requested"

        for stream in streams:
            if stream.attributes & STREAM_MODIFIED_WHEN_READ:
                category = (
                    "extended_attributes"
                    if stream.stream_id == BACKUP_EA_DATA
                    else "alternate_streams"
                )
                merge_coverage(result.coverage, category, "partial")
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category=category,
                        code="windows_stream_modified_when_read",
                        message=(
                            "WIN32_STREAM_ID reported STREAM_MODIFIED_WHEN_READ; "
                            "captured bytes may not equal the pre-read state."
                        ),
                        source_descriptor=source("windows", "BackupRead", "dwStreamAttributes"),
                    )
                )
            if stream.stream_id == BACKUP_EA_DATA:
                if not request.policy.capture_xattrs:
                    continue
                if stream.data is not None:
                    try:
                        rows = self._parse_ea_stream(stream.data, request)
                    except ValueError as exc:
                        rows = []
                        result.coverage["extended_attributes"] = "partial"
                        result.diagnostics.append(
                            diagnostic(
                                severity="error",
                                category="extended_attributes",
                                code="windows_ea_parse_failed",
                                message=str(exc),
                                source_descriptor=source("windows", "BackupRead", "BACKUP_EA_DATA"),
                            )
                        )
                    result.native_metadata.extend(rows)
                    if rows:
                        continue
                result.native_metadata.append(
                    self._backup_stream_row(
                        stream,
                        request=request,
                        kind="windows_extended_attribute",
                        category="extended_attributes",
                        name=stream.name or "$EA",
                        sensitivity="unknown",
                    )
                )
            elif stream.stream_id == BACKUP_ALTERNATE_DATA:
                if request.policy.capture_special_features:
                    result.native_metadata.append(
                        self._backup_stream_row(
                            stream,
                            request=request,
                            kind="alternate_data_stream",
                            category="alternate_streams",
                            name=stream.name or "unnamed-alternate-data-stream",
                            sensitivity="unknown",
                        )
                    )
            elif request.policy.capture_special_features:
                # Other BackupRead metadata streams remain useful evidence even
                # when a more specific handle-bound API also produced a row.
                result.native_metadata.append(
                    self._backup_stream_row(
                        stream,
                        request=request,
                        kind="other",
                        category="special_file_features",
                        name=f"backup-stream-{stream.stream_id}",
                        sensitivity=(
                            "security_sensitive"
                            if stream.attributes & STREAM_CONTAINS_SECURITY
                            else "unknown"
                        ),
                        kind_uri=(
                            "https://nashspence.github.io/riverhog/v1/provenance/observers/vocab/"
                            f"windows-backup-stream/{stream.stream_id}"
                        ),
                    )
                )

    def _backup_stream_row(
        self,
        stream: BackupStreamCapture,
        *,
        request: ObservationRequest,
        kind: str,
        category: str,
        name: str,
        sensitivity: str,
        kind_uri: str | None = None,
    ) -> JsonObject:
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        name_fields = _windows_name_fields(name)
        info = {
            "stream_id": stream.stream_id,
            "stream_attributes": stream.attributes,
            "stream_attribute_names": _stream_attribute_names(stream.attributes),
            "stream_size": stream.size,
        }
        row = _native_row(
            kind=kind,
            category=category,
            name=str(name_fields.pop("name")),
            api="BackupRead",
            field="WIN32_STREAM_ID",
            value=self._stream_value(stream, agent_id=agent_id),
            status=stream.capture_status,
            sensitivity=sensitivity,
            note=stream.note,
            interpretations=[
                {
                    "kind": "structured_parse",
                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-backup-stream-info.json",
                    "value": _schema_value("windows-backup-stream-info", info),
                    "agent_id": agent_id,
                    "confidence": "high",
                }
            ],
            observed_byte_length=stream.size,
        )
        row.update(name_fields)
        if kind_uri is not None:
            row["kind_uri"] = kind_uri
        return row

    def _parse_ea_stream(self, data: bytes, request: ObservationRequest) -> list[JsonObject]:
        rows: list[JsonObject] = []
        position = 0
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        while position < len(data):
            if len(data) - position < 8:
                raise ValueError("truncated FILE_FULL_EA_INFORMATION header")
            next_offset, flags, name_len, value_len = struct.unpack_from("<IBBH", data, position)
            name_start = position + 8
            name_end = name_start + name_len
            value_start = name_end + 1
            value_end = value_start + value_len
            if value_end > len(data) or name_end >= len(data):
                raise ValueError("FILE_FULL_EA_INFORMATION length exceeds stream")
            if data[name_end] != 0:
                raise ValueError("FILE_FULL_EA_INFORMATION name lacks NUL terminator")
            name_raw = data[name_start:name_end]
            value_raw = data[value_start:value_end]
            try:
                name_text = safe_portable_text(name_raw.decode("ascii", "strict"))
                role = "exact"
            except (UnicodeDecodeError, ValueError):
                name_text = "bytes:" + name_raw.hex()
                role = "display"
            status, value, note = retained_native_value(
                value_raw,
                agent_id=agent_id,
                request=request,
            )
            row = _native_row(
                kind="windows_extended_attribute",
                category="extended_attributes",
                namespace="windows.ea",
                name=name_text,
                api="BackupRead",
                field="BACKUP_EA_DATA/FILE_FULL_EA_INFORMATION",
                value=value,
                status=status,
                sensitivity="unknown",
                note=note,
                interpretations=[
                    {
                        "kind": "structured_parse",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-backup-stream-info.json",
                        "value": _schema_value(
                            "windows-backup-stream-info",
                            {
                                "stream_id": BACKUP_EA_DATA,
                                "stream_attributes": 0,
                                "stream_attribute_names": [],
                                "stream_size": len(value_raw),
                                "ea_flags": flags,
                                "need_ea": bool(flags & 0x80),
                            },
                        ),
                        "agent_id": agent_id,
                        "confidence": "high",
                    }
                ],
                observed_byte_length=len(value_raw),
            )
            row["name_bytes"] = {
                "encoding": "base64",
                "data": base64.b64encode(name_raw).decode("ascii"),
                "byte_length": len(name_raw),
            }
            row["name_role"] = role
            if role == "exact":
                row["name_source_encoding"] = "UTF-8"
            rows.append(row)
            if next_offset == 0:
                if value_end != len(data) and any(data[value_end:]):
                    raise ValueError("unexpected nonzero bytes after final EA entry")
                break
            if next_offset % 4 or next_offset < 8:
                raise ValueError("invalid FILE_FULL_EA_INFORMATION NextEntryOffset")
            next_position = position + next_offset
            if next_position <= position or next_position > len(data):
                raise ValueError("invalid FILE_FULL_EA_INFORMATION chain")
            position = next_position
        return rows

    @staticmethod
    def _capture_file_flags(
        stat: NativeStat,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        attributes = int(stat.extras.get("file_attributes", 0))
        data = {
            "bitmask": attributes,
            "hex": f"0x{attributes:08x}",
            "names": _attribute_names(attributes),
            "reparse_tag": int(stat.extras.get("reparse_tag", 0)),
        }
        result.native_metadata.append(
            _native_row(
                kind="file_flag",
                category="file_flags",
                name="file-attribute-bitmask",
                api="GetFileInformationByHandleEx",
                field="FileAttributeTagInfo.FileAttributes",
                value=_schema_value("windows-file-attributes", data),
                interpretations=[
                    {
                        "kind": "structured_parse",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-file-attributes.json",
                        "value": _schema_value("windows-file-attributes", data),
                        "agent_id": request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                        "confidence": "high",
                    }
                ],
            )
        )
        result.coverage["file_flags"] = "complete"

    def _capture_sparse_map(
        self,
        fd: int,
        stat: NativeStat,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        try:
            observed = self.api.allocated_ranges(
                fd,
                stat.size,
                maximum_extents=request.policy.maximum_sparse_extents,
            )
        except OSError as exc:
            error = int(getattr(exc, "winerror", exc.errno or 0))
            result.coverage["storage_layout"] = "partial"
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="storage_layout",
                    code="windows_allocated_ranges_failed",
                    message="Allocated-range enumeration failed.",
                    native_code=str(error),
                    source_descriptor=source(
                        "windows", "DeviceIoControl", "FSCTL_QUERY_ALLOCATED_RANGES"
                    ),
                )
            )
            return
        if observed is None:
            result.coverage["storage_layout"] = "not_supported"
            return
        ranges, complete = observed
        extents: list[dict[str, int | str]] = []
        cursor = 0
        for offset, length in sorted(ranges):
            if offset > cursor:
                extents.append({"kind": "hole", "offset": cursor, "length": offset - cursor})
            end = min(stat.size, offset + length)
            if end > offset:
                extents.append({"kind": "data", "offset": offset, "length": end - offset})
            cursor = max(cursor, end)
        if cursor < stat.size:
            extents.append({"kind": "hole", "offset": cursor, "length": stat.size - cursor})
        map_data = {"complete": complete, "extents": extents}
        result.native_metadata.append(
            _native_row(
                kind="sparse_map",
                category="storage_layout",
                name="allocated-and-unallocated-ranges",
                api="DeviceIoControl",
                field="FSCTL_QUERY_ALLOCATED_RANGES",
                value=_schema_value("sparse-map", map_data),
                interpretations=[
                    {
                        "kind": "structured_parse",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/sparse-map.json",
                        "value": _schema_value("sparse-map", map_data),
                        "agent_id": request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                        "confidence": "high",
                    }
                ],
            )
        )
        result.coverage["storage_layout"] = "complete" if complete else "partial"
        if not complete:
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="storage_layout",
                    code="windows_sparse_extent_limit",
                    message="Allocated-range enumeration exceeded maximum_sparse_extents.",
                    source_descriptor=source(
                        "windows", "DeviceIoControl", "FSCTL_QUERY_ALLOCATED_RANGES"
                    ),
                )
            )

    def _capture_special_features(
        self,
        fd: int,
        stat: NativeStat,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        complete = True
        compression = {
            "file_attribute_compressed": bool(
                int(stat.extras.get("file_attributes", 0)) & FILE_ATTRIBUTE_COMPRESSED
            ),
            "compressed_size": int(stat.extras.get("compressed_size") or stat.size),
            "compression_format": int(stat.extras.get("compression_format") or 0),
            "compression_format_name": COMPRESSION_FORMAT_NAMES.get(
                int(stat.extras.get("compression_format") or 0), "unknown"
            ),
            "compression_unit_shift": int(stat.extras.get("compression_unit_shift") or 0),
            "chunk_shift": int(stat.extras.get("chunk_shift") or 0),
            "cluster_shift": int(stat.extras.get("cluster_shift") or 0),
        }
        result.native_metadata.append(
            _native_row(
                kind="compression_state",
                category="special_file_features",
                name="filesystem-compression-state",
                api="GetFileInformationByHandleEx",
                field="FileCompressionInfo",
                value=_schema_value("windows-compression-state", compression),
                interpretations=[
                    {
                        "kind": "structured_parse",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-compression-state.json",
                        "value": _schema_value("windows-compression-state", compression),
                        "agent_id": agent_id,
                        "confidence": "high",
                    }
                ],
            )
        )
        encrypted = bool(int(stat.extras.get("file_attributes", 0)) & FILE_ATTRIBUTE_ENCRYPTED)
        result.native_metadata.append(
            _native_row(
                kind="encryption_state",
                category="special_file_features",
                name="efs-file-attribute",
                api="GetFileInformationByHandleEx",
                field="FileAttributeTagInfo.FileAttributes",
                value={"type": "boolean", "data": encrypted},
                note=(
                    "This records FILE_ATTRIBUTE_ENCRYPTED only; EFS decryption or "
                    "backup-key material is out of scope."
                ),
            )
        )

        opened = self._opened.get(fd)
        if opened and opened.reparse_data is not None:
            tag_data = {
                "reparse_tag": opened.reparse_tag,
                "reparse_tag_hex": f"0x{opened.reparse_tag:08x}",
                "name_surrogate": _is_name_surrogate(opened.reparse_tag),
                "followed_for_primary_content": not _is_name_surrogate(opened.reparse_tag),
            }
            result.native_metadata.append(
                _native_row(
                    kind="reparse_point",
                    category="special_file_features",
                    name="reparse-data-buffer",
                    api="DeviceIoControl",
                    field="FSCTL_GET_REPARSE_POINT",
                    value=bytes_value(opened.reparse_data, agent_id=agent_id),
                    observed_byte_length=len(opened.reparse_data),
                    interpretations=[
                        {
                            "kind": "structured_parse",
                            "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-reparse-point.json",
                            "value": _schema_value("windows-reparse-point", tag_data),
                            "agent_id": agent_id,
                            "confidence": "high",
                        }
                    ],
                    sensitivity="unknown",
                )
            )

        if request.policy.windows_capture_object_id:
            try:
                object_id = self.api.object_id(fd)
            except OSError as exc:
                complete = False
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="special_file_features",
                        code="windows_object_id_failed",
                        message="FSCTL_GET_OBJECT_ID failed.",
                        native_code=str(getattr(exc, "winerror", exc.errno or 0)),
                        source_descriptor=source(
                            "windows", "DeviceIoControl", "FSCTL_GET_OBJECT_ID"
                        ),
                    )
                )
            else:
                if object_id:
                    data = {
                        "object_id": object_id[:16].hex(),
                        "extended_info": object_id[16:].hex(),
                    }
                    result.native_metadata.append(
                        _native_row(
                            kind="native_stat_field",
                            category="special_file_features",
                            name="file-object-id",
                            api="DeviceIoControl",
                            field="FSCTL_GET_OBJECT_ID",
                            value=bytes_value(object_id, agent_id=agent_id),
                            observed_byte_length=len(object_id),
                            interpretations=[
                                {
                                    "kind": "structured_parse",
                                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-object-id.json",
                                    "value": _schema_value("windows-object-id", data),
                                    "agent_id": agent_id,
                                    "confidence": "high",
                                }
                            ],
                        )
                    )

        try:
            integrity = self.api.integrity_info(fd)
        except OSError as exc:
            complete = False
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="special_file_features",
                    code="windows_integrity_info_failed",
                    message="FSCTL_GET_INTEGRITY_INFORMATION failed.",
                    native_code=str(getattr(exc, "winerror", exc.errno or 0)),
                    source_descriptor=source(
                        "windows", "DeviceIoControl", "FSCTL_GET_INTEGRITY_INFORMATION"
                    ),
                )
            )
        else:
            if integrity is not None:
                data = {
                    "checksum_algorithm": integrity.checksum_algorithm,
                    "flags": integrity.flags,
                    "checksum_chunk_size": integrity.checksum_chunk_size,
                    "cluster_size": integrity.cluster_size,
                }
                result.native_metadata.append(
                    _native_row(
                        kind="native_stat_field",
                        category="special_file_features",
                        name="integrity-information",
                        api="DeviceIoControl",
                        field="FSCTL_GET_INTEGRITY_INFORMATION",
                        value=_schema_value("windows-integrity-info", data),
                        interpretations=[
                            {
                                "kind": "structured_parse",
                                "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-integrity-info.json",
                                "value": _schema_value("windows-integrity-info", data),
                                "agent_id": agent_id,
                                "confidence": "high",
                            }
                        ],
                    )
                )

        usn = stat.extras.get("usn")
        if request.policy.windows_capture_usn and isinstance(usn, Mapping):
            raw = usn.get("raw")
            if isinstance(raw, (bytes, bytearray)):
                parsed = {
                    key: value
                    for key, value in usn.items()
                    if key not in {"raw", "file_name_utf16le"}
                }
                filename = parsed.get("file_name")
                if isinstance(filename, str):
                    portable_name, name_role = _portable_windows_text(filename)
                    parsed["file_name"] = portable_name
                    parsed["file_name_role"] = name_role
                filename_bytes = usn.get("file_name_utf16le")
                if isinstance(filename_bytes, (bytes, bytearray)):
                    parsed["file_name_utf16le_base64"] = base64.b64encode(
                        bytes(filename_bytes)
                    ).decode("ascii")
                result.native_metadata.append(
                    _native_row(
                        kind="native_stat_field",
                        category="special_file_features",
                        name="change-journal-file-record",
                        api="DeviceIoControl",
                        field="FSCTL_READ_FILE_USN_DATA",
                        value=bytes_value(bytes(raw), agent_id=agent_id),
                        observed_byte_length=len(raw),
                        interpretations=[
                            {
                                "kind": "structured_parse",
                                "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-usn-record.json",
                                "value": _schema_value("windows-usn-record", parsed),
                                "agent_id": agent_id,
                                "confidence": "high",
                                "note": (
                                    "Timestamp, Reason, and SourceInfo are omitted because "
                                    "Microsoft documents them as invalid for this control code."
                                ),
                            }
                        ],
                    )
                )
        result.coverage["special_file_features"] = "complete" if complete else "partial"

    @staticmethod
    def _capture_native_stat(
        stat: NativeStat,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        data = {
            "volume_serial_number": int(stat.extras.get("volume_serial_number", 0)),
            "file_id_hex": str(stat.extras.get("file_id_hex", "")),
            "file_id_bits": int(stat.extras.get("file_id_bits", 0)),
            "file_id_scheme": str(stat.extras.get("file_id_scheme", "")),
            "file_index_64": int(stat.extras.get("file_index_64", 0)),
            "creation_time_ticks": int(stat.extras.get("creation_ticks", 0)),
            "last_access_time_ticks": int(stat.extras.get("access_ticks", 0)),
            "last_write_time_ticks": int(stat.extras.get("write_ticks", 0)),
            "change_time_ticks": int(stat.extras.get("change_ticks", 0)),
            "file_attributes": int(stat.extras.get("file_attributes", 0)),
            "reparse_tag": int(stat.extras.get("reparse_tag", 0)),
            "allocation_size": int(stat.extras.get("allocation_size", 0)),
            "end_of_file": stat.size,
            "number_of_links": stat.nlink,
            "delete_pending": bool(stat.extras.get("delete_pending", False)),
            "storage": dict(stat.extras.get("storage") or {}),
        }
        result.native_metadata.append(
            _native_row(
                kind="native_stat_field",
                category="native_metadata_other",
                name="windows-handle-file-information",
                api="GetFileInformationByHandleEx",
                field="FileBasicInfo:FileStandardInfo:FileIdInfo:FileStorageInfo",
                value=_schema_value("windows-file-stat", data),
                interpretations=[
                    {
                        "kind": "structured_parse",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/windows-file-stat.json",
                        "value": _schema_value("windows-file-stat", data),
                        "agent_id": request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                        "confidence": "high",
                    }
                ],
            )
        )

    @staticmethod
    def _volume_context(volume: WindowsVolumeInfo) -> JsonObject:
        return {
            "filesystem_name": volume.filesystem_name,
            "volume_label": volume.volume_label,
            "volume_serial_number": volume.volume_serial_number,
            "maximum_component_length": volume.maximum_component_length,
            "filesystem_flags": volume.filesystem_flags,
            "mount_path": volume.mount_path or "",
            "volume_guid_path": volume.volume_guid_path or "",
            "drive_type": volume.drive_type if volume.drive_type is not None else 0,
            "sectors_per_cluster": volume.sectors_per_cluster or 0,
            "bytes_per_sector": volume.bytes_per_sector or 0,
            "final_path": volume.final_path or "",
        }

    def _environment(
        self,
        stat: NativeStat,
        path: str,
        volume: WindowsVolumeInfo,
        request: ObservationRequest,
    ) -> JsonObject:
        os_info_raw = dict(self.api.os_information())
        host: JsonObject = {
            "id": request.host_id,
            "hardware_architecture": platform.machine() or "unknown",
        }
        if request.policy.include_hostname:
            host["name"] = socket.gethostname()
        os_info: JsonObject = {
            "family": "windows",
            "name": os_info_raw.get("name") or "Microsoft Windows",
            "version": os_info_raw.get("version") or "unknown",
            "kernel": {
                "name": "Windows NT",
                "release": os_info_raw.get("kernel_release") or "unknown",
                "version": os_info_raw.get("kernel_version") or "unknown",
            },
        }
        if os_info_raw.get("build"):
            os_info["build"] = os_info_raw["build"]
        os_identifiers = []
        if os_info_raw.get("edition") and os_info_raw.get("edition") != "unknown":
            os_identifiers.append(
                identifier(
                    scheme="windows-edition-id",
                    value=os_info_raw["edition"],
                    scope="global",
                )
            )
        if (
            os_info_raw.get("installation_type")
            and os_info_raw.get("installation_type") != "unknown"
        ):
            os_identifiers.append(
                identifier(
                    scheme="windows-installation-type",
                    value=os_info_raw["installation_type"],
                    scope="global",
                )
            )
        if os_identifiers:
            os_info["identifiers"] = os_identifiers

        fs_flags = volume.filesystem_flags
        filesystem: JsonObject = {
            "type": (volume.filesystem_name or "unknown").lower(),
            "case_sensitive": bool(fs_flags & FILE_CASE_SENSITIVE_SEARCH),
            "case_preserving": bool(fs_flags & FILE_CASE_PRESERVED_NAMES),
            "name_normalization": "implementation_defined",
            "networked": volume.drive_type == DRIVE_REMOTE,
        }
        if volume.mount_path:
            filesystem["mount_locator"] = windows_locator(
                volume.mount_path,
                kind="absolute",
                authority_id=request.host_id,
            )
        volume_ids: list[JsonObject] = [
            identifier(
                scheme="windows-volume-serial-number",
                value=f"{volume.volume_serial_number:08x}",
                scope="host",
                authority_id=request.host_id,
            )
        ]
        if volume.volume_guid_path:
            volume_ids.insert(
                0,
                identifier(
                    scheme="windows-volume-guid-path",
                    value=volume.volume_guid_path,
                    scope="host",
                    authority_id=request.host_id,
                ),
            )
        filesystem["volume_identifiers"] = volume_ids

        principal, privilege = self.api.current_token(resolve=request.policy.resolve_principals)
        runtime: JsonObject = {
            "process_architecture": platform.machine() or "unknown",
            "time_zone": dt.datetime.now().astimezone().tzname() or "unknown",
            "utc_offset": utc_offset_string(),
            "character_encoding": locale.getpreferredencoding(False) or "UTF-8",
            "privilege": privilege,
        }
        loc = locale.setlocale(locale.LC_CTYPE, None)
        if loc:
            runtime["locale"] = safe_portable_text(loc)
        if request.policy.include_effective_principal:
            principal_doc = _principal_document(principal, "user")
            if principal_doc:
                runtime["effective_principal"] = principal_doc
        return {
            "id": "urn:uuid:00000000-0000-0000-0000-000000000000",
            "type": "technical_environment",
            "host": host,
            "operating_system": os_info,
            "filesystem": filesystem,
            "runtime": runtime,
        }


class WindowsFileStateObserver(DescriptorFileStateObserver):
    """Archive-level Riverhog provenance observer for Windows 11 25H2/26H1 and later."""

    def __init__(
        self,
        *,
        native: WindowsNativeAPI | Any | None = None,
        enforce_platform: bool = True,
    ) -> None:
        super().__init__(WindowsBackend(native=native, enforce_platform=enforce_platform))
