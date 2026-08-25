from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import os
import platform
import socket
import struct
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from riverhog_provenance.common import (
    DescriptorFileStateObserver,
    basic_access,
    bytes_value,
    diagnostic,
    digest_assertion,
    identifier,
    locator_from_path,
    make_sparse_map_row,
    merge_coverage,
    native_name_fields,
    observed_identifier,
    portable_text_from_bytes,
    retained_native_value,
    runtime_environment,
    source,
    sparse_extents,
    timestamp_observation,
)
from riverhog_provenance.constants import DEFAULT_OBSERVER_AGENT_ID, OBSERVER_NAMESPACE
from riverhog_provenance.errors import (
    NativeObservationError,
    SymlinkRefusedError,
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
)
from riverhog_provenance_macos_contracts import PLATFORM_FAMILY

# Darwin getattrlist(2) constants, from XNU sys/attr.h.
ATTR_BIT_MAP_COUNT = 5
ATTR_CMN_CRTIME = 0x00000200
ATTR_CMN_MODTIME = 0x00000400
ATTR_CMN_CHGTIME = 0x00000800
ATTR_CMN_ACCTIME = 0x00001000
ATTR_CMN_BKUPTIME = 0x00002000
ATTR_CMN_FNDRINFO = 0x00004000
ATTR_CMN_FLAGS = 0x00040000
ATTR_CMN_GEN_COUNT = 0x00080000
ATTR_CMN_DOCUMENT_ID = 0x00100000
ATTR_CMN_FILEID = 0x02000000
ATTR_CMN_PARENTID = 0x04000000
ATTR_CMN_ADDEDTIME = 0x10000000
ATTR_FILE_LINKCOUNT = 0x00000001
ATTR_FILE_TOTALSIZE = 0x00000002
ATTR_FILE_ALLOCSIZE = 0x00000004
ATTR_FILE_IOBLOCKSIZE = 0x00000008
ATTR_FILE_DATALENGTH = 0x00000200
ATTR_FILE_DATAALLOCSIZE = 0x00000400
ATTR_FILE_RSRCLENGTH = 0x00001000
ATTR_FILE_RSRCALLOCSIZE = 0x00002000
ATTR_VOL_INFO = 0x80000000
ATTR_VOL_CAPABILITIES = 0x00020000
ATTR_VOL_UUID = 0x00040000
FSOPT_ATTR_CMN_EXTENDED = 0x00000020

ACL_TYPE_EXTENDED = 0x00000100

_VOL_CAP_FMT_CASE_SENSITIVE = 0x00000100
_VOL_CAP_FMT_CASE_PRESERVING = 0x00000200
_VOL_CAP_INT_EXTENDED_SECURITY = 0x00000400
_VOL_CAP_INT_EXTENDED_ATTR = 0x00002000
_VOL_CAP_INT_NAMEDSTREAMS = 0x00004000

_DARWIN_FLAG_NAMES = {
    0x00000001: "user_nodump",
    0x00000002: "user_immutable",
    0x00000004: "user_append_only",
    0x00000008: "user_opaque",
    0x00000020: "user_compressed",
    0x00000040: "user_tracked",
    0x00000080: "user_datavault",
    0x00008000: "user_hidden",
    0x00010000: "system_archived",
    0x00020000: "system_immutable",
    0x00040000: "system_append_only",
    0x00080000: "system_restricted",
    0x00100000: "system_nounlink",
    0x00800000: "system_firmlink",
    0x40000000: "system_dataless",
}

_NETWORK_FILESYSTEMS = {
    "afpfs",
    "cifs",
    "nfs",
    "smbfs",
    "webdav",
}


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class _Fsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class _DarwinStatFs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", _Fsid),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


@dataclass(frozen=True, slots=True)
class DarwinFileSystemInfo:
    fs_type: str
    mount_point: bytes
    mounted_from: bytes
    fsid: tuple[int, int]
    flags: int
    subtype: int
    io_size: int
    block_size: int


@dataclass(frozen=True, slots=True)
class ACLCapture:
    raw: bytes
    text: str | None


class MacOSNativeAPI:
    """ctypes boundary for current macOS descriptor and volume metadata APIs."""

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self._configure()

    def _configure(self) -> None:
        self.libc.flistxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        self.libc.flistxattr.restype = ctypes.c_ssize_t
        self.libc.fgetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self.libc.fgetxattr.restype = ctypes.c_ssize_t
        self.libc.fgetattrlist.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_AttrList),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_ulong,
        ]
        self.libc.fgetattrlist.restype = ctypes.c_int
        self.libc.getattrlist.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(_AttrList),
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_ulong,
        ]
        self.libc.getattrlist.restype = ctypes.c_int
        self.libc.fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_DarwinStatFs)]
        self.libc.fstatfs.restype = ctypes.c_int
        if hasattr(self.libc, "sysctlbyname"):
            self.libc.sysctlbyname.argtypes = [
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            self.libc.sysctlbyname.restype = ctypes.c_int
        self._configure_acl()

    def _configure_acl(self) -> None:
        if not hasattr(self.libc, "acl_get_fd_np"):
            return
        self.libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.acl_get_fd_np.restype = ctypes.c_void_p
        self.libc.acl_size.argtypes = [ctypes.c_void_p]
        self.libc.acl_size.restype = ctypes.c_ssize_t
        copy_name = "acl_copy_ext" if hasattr(self.libc, "acl_copy_ext") else "acl_copy_ext_native"
        self._acl_copy = getattr(self.libc, copy_name)
        self._acl_copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
        self._acl_copy.restype = ctypes.c_ssize_t
        self.libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        self.libc.acl_to_text.restype = ctypes.c_void_p
        self.libc.acl_free.argtypes = [ctypes.c_void_p]
        self.libc.acl_free.restype = ctypes.c_int

    def list_xattrs(self, fd: int) -> list[bytes]:
        for _ in range(4):
            required = self.libc.flistxattr(fd, None, 0, 0)
            if required < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            if required == 0:
                return []
            buffer = ctypes.create_string_buffer(required)
            actual = self.libc.flistxattr(fd, buffer, required, 0)
            if actual >= 0:
                return [part for part in bytes(buffer.raw[:actual]).split(b"\0") if part]
            error = ctypes.get_errno()
            if error != errno.ERANGE:
                raise OSError(error, os.strerror(error))
        raise OSError(errno.ERANGE, "xattr name list changed repeatedly")

    def xattr_size(self, fd: int, name: bytes) -> int:
        result = self.libc.fgetxattr(fd, name, None, 0, 0, 0)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return int(result)

    def get_xattr(self, fd: int, name: bytes, maximum: int) -> tuple[int, bytes | None]:
        for _ in range(4):
            required = self.xattr_size(fd, name)
            if required > maximum:
                return required, None
            if required == 0:
                return 0, b""
            buffer = ctypes.create_string_buffer(required)
            actual = self.libc.fgetxattr(fd, name, buffer, required, 0, 0)
            if actual >= 0:
                return int(actual), bytes(buffer.raw[:actual])
            error = ctypes.get_errno()
            if error != errno.ERANGE:
                raise OSError(error, os.strerror(error))
        raise OSError(errno.ERANGE, f"xattr {name!r} changed repeatedly")

    def digest_resource_fork(self, fd: int, name: bytes, *, chunk_bytes: int) -> tuple[int, str]:
        size = self.xattr_size(fd, name)
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            length = min(chunk_bytes, size - offset)
            buffer = ctypes.create_string_buffer(length)
            actual = self.libc.fgetxattr(fd, name, buffer, length, offset, 0)
            if actual < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            if actual == 0:
                raise OSError(errno.EIO, "resource-fork read ended early")
            digest.update(bytes(buffer.raw[:actual]))
            offset += int(actual)
        return size, digest.hexdigest()

    def get_acl(self, fd: int) -> ACLCapture | None:
        if not hasattr(self.libc, "acl_get_fd_np"):
            return None
        ctypes.set_errno(0)
        acl = self.libc.acl_get_fd_np(fd, ACL_TYPE_EXTENDED)
        if not acl:
            error = ctypes.get_errno()
            if error in {0, errno.ENOENT, getattr(errno, "ENOATTR", errno.ENOENT)}:
                return ACLCapture(raw=b"", text=None)
            raise OSError(error, os.strerror(error))
        try:
            size = self.libc.acl_size(acl)
            if size < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            raw_buffer = ctypes.create_string_buffer(size)
            copied = self._acl_copy(raw_buffer, acl, size)
            if copied < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            # Darwin ACL copy routines have historically exposed both the
            # zero-on-success and byte-count-on-success conventions.  The
            # preceding acl_size(3) result bounds the external form.
            raw_length = size if copied == 0 else copied
            if raw_length > size:
                raise OSError(errno.EOVERFLOW, "ACL external form exceeded acl_size")
            raw = bytes(raw_buffer.raw[:raw_length])
            text_length = ctypes.c_ssize_t()
            text_pointer = self.libc.acl_to_text(acl, ctypes.byref(text_length))
            text: str | None = None
            if text_pointer:
                try:
                    text_bytes = ctypes.string_at(text_pointer, max(0, text_length.value))
                    text = text_bytes.decode("utf-8", "replace")
                finally:
                    self.libc.acl_free(text_pointer)
            return ACLCapture(raw=raw, text=text)
        finally:
            self.libc.acl_free(acl)

    @staticmethod
    def _attrlist(*, common: int = 0, volume: int = 0, file: int = 0) -> _AttrList:
        result = _AttrList()
        result.bitmapcount = ATTR_BIT_MAP_COUNT
        result.commonattr = common
        result.volattr = volume
        result.fileattr = file
        return result

    def _get_fixed_fd(
        self,
        fd: int,
        *,
        common: int = 0,
        file: int = 0,
        length: int,
        options: int = 0,
    ) -> bytes:
        attrs = self._attrlist(common=common, file=file)
        buffer = ctypes.create_string_buffer(4 + length + 16)
        rc = self.libc.fgetattrlist(fd, ctypes.byref(attrs), buffer, len(buffer), options)
        if rc != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        returned = struct.unpack_from("=I", buffer.raw, 0)[0]
        if returned < 4 + length:
            # Unsupported requested attributes are skipped unless callers ask
            # for packed invalid values.  For a single fixed attribute, a
            # length-only result therefore means "not available", not corruption.
            raise OSError(errno.ENOTSUP, "requested fgetattrlist attribute unavailable")
        return bytes(buffer.raw[4 : 4 + length])

    def _get_fixed_path(
        self,
        path: bytes,
        *,
        volume: int,
        length: int,
        options: int = 0,
    ) -> bytes:
        attrs = self._attrlist(volume=volume | ATTR_VOL_INFO)
        buffer = ctypes.create_string_buffer(4 + length + 16)
        rc = self.libc.getattrlist(path, ctypes.byref(attrs), buffer, len(buffer), options)
        if rc != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        returned = struct.unpack_from("=I", buffer.raw, 0)[0]
        if returned < 4 + length:
            raise OSError(errno.ENOTSUP, "requested getattrlist attribute unavailable")
        return bytes(buffer.raw[4 : 4 + length])

    def file_attributes(self, fd: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        times = {
            "birthtime_ns": ATTR_CMN_CRTIME,
            "mtime_ns": ATTR_CMN_MODTIME,
            "ctime_ns": ATTR_CMN_CHGTIME,
            "atime_ns": ATTR_CMN_ACCTIME,
            "backup_time_ns": ATTR_CMN_BKUPTIME,
            "added_time_ns": ATTR_CMN_ADDEDTIME,
        }
        for name, bit in times.items():
            try:
                raw = self._get_fixed_fd(fd, common=bit, length=16)
            except OSError as exc:
                if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    continue
                raise
            seconds, nanoseconds = struct.unpack_from("=qq", raw)
            result[name] = int(seconds) * 1_000_000_000 + int(nanoseconds)
        fixed_common = {
            "flags": (ATTR_CMN_FLAGS, "=I", 4, 0),
            "generation": (ATTR_CMN_GEN_COUNT, "=I", 4, FSOPT_ATTR_CMN_EXTENDED),
            "document_id": (ATTR_CMN_DOCUMENT_ID, "=I", 4, FSOPT_ATTR_CMN_EXTENDED),
            "file_id": (ATTR_CMN_FILEID, "=Q", 8, 0),
            "parent_id": (ATTR_CMN_PARENTID, "=Q", 8, 0),
        }
        for name, (bit, fmt, length, options) in fixed_common.items():
            try:
                raw = self._get_fixed_fd(fd, common=bit, length=length, options=options)
            except OSError as exc:
                if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    continue
                raise
            result[name] = int(struct.unpack_from(fmt, raw)[0])
        try:
            result["finder_info"] = self._get_fixed_fd(fd, common=ATTR_CMN_FNDRINFO, length=32)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
        fixed_file = {
            "link_count": (ATTR_FILE_LINKCOUNT, "=I", 4),
            "total_size": (ATTR_FILE_TOTALSIZE, "=Q", 8),
            "allocation_size": (ATTR_FILE_ALLOCSIZE, "=Q", 8),
            "io_block_size": (ATTR_FILE_IOBLOCKSIZE, "=I", 4),
            "data_length": (ATTR_FILE_DATALENGTH, "=Q", 8),
            "data_allocation_size": (ATTR_FILE_DATAALLOCSIZE, "=Q", 8),
            "resource_fork_length": (ATTR_FILE_RSRCLENGTH, "=Q", 8),
            "resource_fork_allocation_size": (ATTR_FILE_RSRCALLOCSIZE, "=Q", 8),
        }
        for name, (bit, fmt, length) in fixed_file.items():
            try:
                raw = self._get_fixed_fd(fd, file=bit, length=length)
            except OSError as exc:
                if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    continue
                raise
            result[name] = int(struct.unpack_from(fmt, raw)[0])
        return result

    def filesystem_info(self, fd: int) -> DarwinFileSystemInfo:
        result = _DarwinStatFs()
        if self.libc.fstatfs(fd, ctypes.byref(result)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return DarwinFileSystemInfo(
            fs_type=bytes(result.f_fstypename).split(b"\0", 1)[0].decode("utf-8", "replace"),
            mount_point=bytes(result.f_mntonname).split(b"\0", 1)[0],
            mounted_from=bytes(result.f_mntfromname).split(b"\0", 1)[0],
            fsid=(int(result.f_fsid.val[0]), int(result.f_fsid.val[1])),
            flags=int(result.f_flags),
            subtype=int(result.f_fssubtype),
            io_size=int(result.f_iosize),
            block_size=int(result.f_bsize),
        )

    def volume_attributes(self, mount_point: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            raw_uuid = self._get_fixed_path(mount_point, volume=ATTR_VOL_UUID, length=16)
            result["uuid"] = str(uuid.UUID(bytes=raw_uuid))
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
        try:
            raw_caps = self._get_fixed_path(mount_point, volume=ATTR_VOL_CAPABILITIES, length=32)
            capabilities = list(struct.unpack_from("=4I", raw_caps, 0))
            valid = list(struct.unpack_from("=4I", raw_caps, 16))
            result["capabilities"] = capabilities
            result["valid_capabilities"] = valid
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
        return result

    def sysctl_text(self, name: str) -> str | None:
        if not hasattr(self.libc, "sysctlbyname"):
            return None
        size = ctypes.c_size_t()
        if self.libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if self.libc.sysctlbyname(name.encode(), buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return bytes(buffer.raw[: size.value]).rstrip(b"\0").decode("utf-8", "replace")


def _flag_names(value: int) -> list[str]:
    return [name for bit, name in _DARWIN_FLAG_NAMES.items() if value & bit]


def _classify_xattr(name: bytes) -> tuple[str, str, str, str]:
    text = name.decode("utf-8", "replace")
    namespace = (
        ".".join(text.split(".")[:2]) if text.startswith("com.apple.") else text.split(".", 1)[0]
    )
    if name == b"com.apple.ResourceFork":
        return "resource_fork", "resource_forks", namespace, "unknown"
    if name == b"com.apple.FinderInfo":
        return "finder_info", "extended_attributes", namespace, "unknown"
    if name == b"com.apple.decmpfs":
        return "compression_state", "special_file_features", namespace, "unknown"
    if name in {
        b"com.apple.macl",
        b"com.apple.provenance",
        b"com.apple.rootless",
        b"com.apple.quarantine",
    }:
        sensitivity = "personal" if name == b"com.apple.quarantine" else "security_sensitive"
        return "security_label", "security_metadata", namespace, sensitivity
    sensitivity = "personal" if name.startswith(b"com.apple.metadata:") else "unknown"
    return "extended_attribute", "extended_attributes", namespace, sensitivity


class MacOSBackend(PlatformBackend):
    platform_family = PLATFORM_FAMILY

    def __init__(
        self,
        *,
        native: MacOSNativeAPI | Any | None = None,
        enforce_platform: bool = True,
    ) -> None:
        self.native = native
        self.enforce_platform = enforce_platform

    def assert_supported(self) -> None:
        if self.enforce_platform and sys.platform != "darwin":
            raise UnsupportedPlatformError("macOS observer requires Darwin/macOS")
        if self.native is None:
            self.native = MacOSNativeAPI()

    @property
    def api(self) -> Any:
        if self.native is None:
            raise RuntimeError("macOS native API is unavailable before platform validation")
        return self.native

    def open_readonly(
        self, path: str | bytes, request: ObservationRequest
    ) -> tuple[int, list[JsonObject], bool]:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SymlinkRefusedError("final path component is a symlink") from exc
            raise
        return fd, [], False

    def stat_fd(self, fd: int) -> NativeStat:
        result = os.fstat(fd)
        try:
            attributes = self.api.file_attributes(fd)
        except OSError:
            attributes = {}
        birthtime_ns = attributes.get("birthtime_ns")
        if birthtime_ns is None and hasattr(result, "st_birthtime"):
            birthtime_ns = int(result.__getattribute__("st_birthtime") * 1_000_000_000)
        return NativeStat(
            device=result.st_dev,
            inode=result.st_ino,
            mode=result.st_mode,
            nlink=result.st_nlink,
            uid=result.st_uid,
            gid=result.st_gid,
            size=result.st_size,
            atime_ns=int(attributes.get("atime_ns", result.st_atime_ns)),
            mtime_ns=int(attributes.get("mtime_ns", result.st_mtime_ns)),
            ctime_ns=int(attributes.get("ctime_ns", result.st_ctime_ns)),
            birthtime_ns=(int(birthtime_ns) if birthtime_ns is not None else None),
            blocks=getattr(result, "st_blocks", None),
            block_size=getattr(result, "st_blksize", None),
            flags=int(attributes.get("flags", getattr(result, "st_flags", 0))),
            generation=(
                int(attributes["generation"])
                if attributes.get("generation") is not None
                else getattr(result, "st_gen", None)
            ),
            rdev=result.st_rdev,
            extras=attributes,
        )

    def collect(
        self,
        fd: int,
        path: str | bytes,
        stat: NativeStat,
        request: ObservationRequest,
    ) -> NativeCollection:
        result = NativeCollection()
        result.timestamps = self._timestamps(stat, request)
        result.access = basic_access(stat, request)
        result.coverage.update(
            {
                "timestamps": "complete",
                "ownership": "complete",
                "permissions": "complete",
                "native_identifiers": "complete",
                "alternate_streams": "not_applicable",
            }
        )
        volume_attrs: dict[str, Any]
        try:
            fs_info = self.api.filesystem_info(fd)
        except OSError as exc:
            fs_info = None
            volume_attrs = {}
            merge_coverage(result.coverage, "native_identifiers", "partial")
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="native_identifiers",
                    code="filesystem_context_unavailable",
                    message="Could not capture macOS descriptor filesystem context.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("macos", "fstatfs(2)"),
                )
            )
        else:
            try:
                volume_attrs = self.api.volume_attributes(fs_info.mount_point)
            except OSError as exc:
                volume_attrs = {}
                merge_coverage(result.coverage, "native_identifiers", "partial")
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="native_identifiers",
                        code="volume_attributes_unavailable",
                        message=(
                            "Filesystem context was captured, but optional macOS "
                            "volume attributes could not be read."
                        ),
                        native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                        source_descriptor=source("macos", "getattrlist(2)"),
                    )
                )
        volume_authority = self._volume_authority(request.host_id, stat, fs_info, volume_attrs)
        result.native_identifiers = self._native_identifiers(
            stat, request.host_id, volume_authority
        )

        if request.policy.capture_xattrs:
            self._capture_xattrs(fd, request, result)
        else:
            for category in (
                "extended_attributes",
                "resource_forks",
                "security_metadata",
            ):
                result.coverage[category] = "not_requested"

        if request.policy.capture_acl:
            self._capture_acl(fd, request, result)
        else:
            result.coverage["access_control"] = "not_requested"

        if request.policy.capture_file_flags:
            self._capture_file_flags(stat, request, result)
        else:
            result.coverage["file_flags"] = "not_requested"

        if request.policy.capture_sparse_map:
            self._capture_sparse_map(fd, stat, request, result)
        else:
            result.coverage["storage_layout"] = "not_requested"

        if request.policy.capture_special_features:
            self._capture_special_features(stat, request, result)
        else:
            result.coverage.setdefault("special_file_features", "not_requested")

        if request.policy.capture_native_stat:
            self._capture_native_stat(stat, request, result)
            # FinderInfo is returned as a fixed 32-byte common attribute even when
            # it is not surfaced in a generic xattr listing.
            finder_info = stat.extras.get("finder_info")
            if isinstance(finder_info, bytes) and any(finder_info):
                result.native_metadata.append(
                    {
                        "kind": "finder_info",
                        "coverage_category": "native_metadata_other",
                        "namespace": "com.apple",
                        "name": "FinderInfo",
                        "capture_status": "captured",
                        "source": source("macos", "fgetattrlist(2)", "ATTR_CMN_FNDRINFO"),
                        "observed_byte_length": len(finder_info),
                        "value": bytes_value(
                            finder_info,
                            agent_id=request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                        ),
                        "sensitivity": "unknown",
                    }
                )
            merge_coverage(result.coverage, "native_metadata_other", "complete")
        else:
            result.coverage.setdefault("native_metadata_other", "not_requested")

        result.environment = self._environment(fs_info, volume_attrs, request)
        if fs_info is not None:
            volume_context: dict[str, Any] = {
                "filesystem_type": fs_info.fs_type or "unknown",
                "mount_point": portable_text_from_bytes(fs_info.mount_point),
                "mounted_from": portable_text_from_bytes(fs_info.mounted_from),
                "fsid": [fs_info.fsid[0], fs_info.fsid[1]],
                "mount_flags": fs_info.flags,
                "filesystem_subtype": fs_info.subtype,
                "io_size": fs_info.io_size,
                "block_size": fs_info.block_size,
            }
            if volume_attrs.get("uuid"):
                volume_context["volume_uuid"] = volume_attrs["uuid"]
            if volume_attrs.get("capabilities") is not None:
                volume_context["capabilities"] = volume_attrs["capabilities"]
            if volume_attrs.get("valid_capabilities") is not None:
                volume_context["valid_capabilities"] = volume_attrs["valid_capabilities"]
            result.extension_drafts.append(
                ExtensionDraft(
                    subject_role="environment",
                    property="https://nashspence.github.io/riverhog/v1/provenance/observers/vocab/macos-volume-context",
                    value={
                        "type": "json",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/macos-volume-context.json",
                        "data": volume_context,
                    },
                    note="Descriptor volume context from fstatfs(2) and getattrlist(2).",
                )
            )
        return result

    @staticmethod
    def _timestamps(stat: NativeStat, request: ObservationRequest) -> list[JsonObject]:
        field_map = [
            ("content_modified", stat.mtime_ns, "ATTR_CMN_MODTIME"),
            ("metadata_changed", stat.ctime_ns, "ATTR_CMN_CHGTIME"),
        ]
        if request.policy.include_access_time:
            field_map.append(("accessed", stat.atime_ns, "ATTR_CMN_ACCTIME"))
        if stat.birthtime_ns is not None:
            field_map.append(("created", stat.birthtime_ns, "ATTR_CMN_CRTIME"))
        if stat.extras.get("backup_time_ns") is not None:
            field_map.append(("backup", int(stat.extras["backup_time_ns"]), "ATTR_CMN_BKUPTIME"))
        if stat.extras.get("added_time_ns") is not None:
            field_map.append(
                (
                    "other",
                    int(stat.extras["added_time_ns"]),
                    "ATTR_CMN_ADDEDTIME",
                )
            )
        observations: list[JsonObject] = []
        for kind, value, field in field_map:
            item = timestamp_observation(
                kind=kind,
                epoch_ns=value,
                platform="macos",
                api="fgetattrlist(2)",
                field=field,
            )
            if kind == "other":
                item["kind_uri"] = (
                    "https://nashspence.github.io/riverhog/v1/provenance/observers/vocab/macos-date-added"
                )
            observations.append(item)
        return observations

    @staticmethod
    def _volume_authority(
        host_id: str,
        stat: NativeStat,
        fs_info: DarwinFileSystemInfo | None,
        volume_attrs: dict[str, Any],
    ) -> str:
        volume_uuid = volume_attrs.get("uuid")
        if volume_uuid:
            # A reported volume UUID is already globally scoped.  Do not make
            # the authority host-dependent when the same volume is moved.
            key = f"macos-volume-uuid:{volume_uuid}"
        else:
            local_key = f"{fs_info.fsid[0]}:{fs_info.fsid[1]}" if fs_info else str(stat.device)
            key = f"macos-volume-local:{host_id}:{local_key}"
        return f"urn:uuid:{uuid.uuid5(OBSERVER_NAMESPACE, key)}"

    @staticmethod
    def _native_identifiers(
        stat: NativeStat, host_id: str, volume_authority: str
    ) -> list[JsonObject]:
        identifiers = [
            observed_identifier(
                scheme="darwin-file-id",
                value=str(stat.extras.get("file_id", stat.inode)),
                scope="filesystem",
                authority_id=volume_authority,
                platform="macos",
                api="fgetattrlist(2)",
                field="ATTR_CMN_FILEID",
            ),
            observed_identifier(
                scheme="darwin-device-number",
                value=str(stat.device),
                scope="host",
                authority_id=host_id,
                platform="macos",
                api="fstat(2)",
                field="st_dev",
            ),
        ]
        if stat.extras.get("parent_id") is not None:
            identifiers.append(
                observed_identifier(
                    scheme="darwin-parent-file-id",
                    value=str(stat.extras["parent_id"]),
                    scope="filesystem",
                    authority_id=volume_authority,
                    platform="macos",
                    api="fgetattrlist(2)",
                    field="ATTR_CMN_PARENTID",
                )
            )
        if stat.extras.get("document_id"):
            identifiers.append(
                observed_identifier(
                    scheme="darwin-document-id",
                    value=str(stat.extras["document_id"]),
                    scope="volume",
                    authority_id=volume_authority,
                    platform="macos",
                    api="fgetattrlist(2)",
                    field="ATTR_CMN_DOCUMENT_ID",
                )
            )
        return identifiers

    def _capture_xattrs(
        self, fd: int, request: ObservationRequest, result: NativeCollection
    ) -> None:
        try:
            names = self.api.list_xattrs(fd)
        except OSError as exc:
            for category in (
                "extended_attributes",
                "resource_forks",
                "security_metadata",
                "special_file_features",
            ):
                merge_coverage(result.coverage, category, "failed")
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="extended_attributes",
                    code="flistxattr_failed",
                    message="Could not enumerate macOS extended attributes.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("macos", "flistxattr(2)"),
                )
            )
            return
        had_error: dict[str, bool] = {
            "extended_attributes": False,
            "resource_forks": False,
            "security_metadata": False,
            "special_file_features": False,
            "native_metadata_other": False,
        }
        seen: set[str] = set()
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        for name in sorted(names):
            kind, category, namespace, sensitivity = _classify_xattr(name)
            seen.add(category)
            fields = native_name_fields(name)
            row: JsonObject = {
                "kind": kind,
                "coverage_category": category,
                "namespace": namespace,
                **fields,
                "capture_status": "captured",
                "source": source("macos", "fgetxattr(2)", fields["name"]),
                "sensitivity": sensitivity,
            }
            try:
                if name == b"com.apple.ResourceFork":
                    self._capture_resource_fork(fd, name, row, request)
                else:
                    observed_length, data = self.api.get_xattr(
                        fd, name, request.policy.maximum_native_value_bytes
                    )
                    row["observed_byte_length"] = observed_length
                    if data is None:
                        if request.policy.large_value_disposition is LargeValueDisposition.FAIL:
                            raise NativeObservationError(
                                f"xattr {name!r} exceeds maximum_native_value_bytes"
                            )
                        row["capture_status"] = "not_retained"
                        row["note"] = "Value exceeded maximum_native_value_bytes."
                    else:
                        status, value, note = retained_native_value(
                            data, agent_id=agent_id, request=request
                        )
                        row["capture_status"] = status
                        if value is not None:
                            row["value"] = value
                        if note:
                            row["note"] = note
            except OSError as exc:
                row["capture_status"] = "unreadable"
                row["note"] = f"fgetxattr failed: {os.strerror(exc.errno or 0)}"
                had_error[category] = True
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category=category,
                        code="fgetxattr_failed",
                        message=f"Could not read xattr {fields['name']}.",
                        native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                        source_descriptor=source("macos", "fgetxattr(2)", fields["name"]),
                    )
                )
            result.native_metadata.append(row)
        for category in ("extended_attributes", "resource_forks", "security_metadata"):
            merge_coverage(
                result.coverage,
                category,
                "partial" if had_error[category] else "complete",
            )
        for category in ("special_file_features", "native_metadata_other"):
            if category in seen:
                merge_coverage(
                    result.coverage,
                    category,
                    "partial" if had_error[category] else "complete",
                )

    def _capture_resource_fork(
        self,
        fd: int,
        name: bytes,
        row: JsonObject,
        request: ObservationRequest,
    ) -> None:
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        size = self.api.xattr_size(fd, name)
        row["observed_byte_length"] = size
        if size <= request.policy.inline_native_value_bytes:
            _, data = self.api.get_xattr(fd, name, request.policy.maximum_native_value_bytes)
            if data is None:
                raise OSError(errno.EFBIG, "resource fork exceeded configured maximum")
            row["capture_status"] = "captured"
            row["value"] = bytes_value(data, agent_id=agent_id)
            return
        disposition = request.policy.large_value_disposition
        if disposition is LargeValueDisposition.FAIL:
            raise NativeObservationError("resource fork exceeds inline retention threshold")
        if disposition is LargeValueDisposition.NOT_RETAINED:
            row["capture_status"] = "not_retained"
            row["note"] = "Resource fork exceeded inline threshold and was not retained."
            return
        observed, sha256 = self.api.digest_resource_fork(
            fd, name, chunk_bytes=request.policy.resource_fork_chunk_bytes
        )
        row["observed_byte_length"] = observed
        row["capture_status"] = "digest_only"
        row["value"] = {
            "type": "digest",
            "byte_length": observed,
            "digests": [
                digest_assertion(
                    sha256,
                    agent_id=agent_id,
                    purpose="native_metadata",
                )
            ],
        }

    def _capture_acl(self, fd: int, request: ObservationRequest, result: NativeCollection) -> None:
        if not hasattr(self.native, "get_acl"):
            merge_coverage(result.coverage, "access_control", "not_supported")
            return
        try:
            captured = self.api.get_acl(fd)
        except OSError as exc:
            merge_coverage(result.coverage, "access_control", "failed")
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="access_control",
                    code="acl_get_fd_np_failed",
                    message="Could not read the macOS extended ACL.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("macos", "acl_get_fd_np(3)", "ACL_TYPE_EXTENDED"),
                )
            )
            return
        merge_coverage(result.coverage, "access_control", "complete")
        if captured is None or (captured.raw == b"" and captured.text is None):
            return
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        status, value, note = retained_native_value(
            captured.raw, agent_id=agent_id, request=request
        )
        row: JsonObject = {
            "kind": "acl",
            "coverage_category": "access_control",
            "namespace": "darwin.extended-acl",
            "name": "extended-acl",
            "capture_status": status,
            "source": source("macos", "acl_get_fd_np(3)", "ACL_TYPE_EXTENDED"),
            "observed_byte_length": len(captured.raw),
            "sensitivity": "security_sensitive",
        }
        if value is not None:
            row["value"] = value
        if note:
            row["note"] = note
        if captured.text is not None:
            encoded = captured.text.encode("utf-8")
            row["interpretations"] = [
                {
                    "kind": "text_decode",
                    "value": {
                        "type": "text",
                        "data": captured.text,
                        "source_encoding": "UTF-8",
                        "byte_length": len(encoded),
                        "media_type": "text/plain",
                    },
                    "agent_id": agent_id,
                    "confidence": "high",
                    "note": "Text generated by acl_to_text(3).",
                }
            ]
        result.native_metadata.append(row)

    @staticmethod
    def _capture_file_flags(
        stat: NativeStat, request: ObservationRequest, result: NativeCollection
    ) -> None:
        if stat.flags is None:
            merge_coverage(result.coverage, "file_flags", "not_supported")
            return
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        data = {"raw": stat.flags, "set_names": _flag_names(stat.flags)}
        result.native_metadata.append(
            {
                "kind": "file_flag",
                "coverage_category": "file_flags",
                "namespace": "darwin.stat",
                "name": "st_flags",
                "capture_status": "captured",
                "source": source("macos", "fgetattrlist(2)", "ATTR_CMN_FLAGS"),
                "value": {"type": "integer", "data": str(stat.flags)},
                "interpretations": [
                    {
                        "kind": "structured_parse",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/darwin-file-flags.json",
                        "value": {
                            "type": "json",
                            "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/darwin-file-flags.json",
                            "data": data,
                        },
                        "agent_id": agent_id,
                        "confidence": "high",
                    }
                ],
                "sensitivity": "public",
            }
        )
        merge_coverage(result.coverage, "file_flags", "complete")

    @staticmethod
    def _capture_sparse_map(
        fd: int,
        stat: NativeStat,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        try:
            extents, complete = sparse_extents(
                fd,
                stat.size,
                maximum_extents=request.policy.maximum_sparse_extents,
            )
        except OSError as exc:
            if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENXIO}:
                merge_coverage(result.coverage, "storage_layout", "not_supported")
                return
            merge_coverage(result.coverage, "storage_layout", "failed")
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="storage_layout",
                    code="seek_data_hole_failed",
                    message="Could not enumerate macOS sparse extents.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("macos", "lseek(2)", "SEEK_DATA/SEEK_HOLE"),
                )
            )
            return
        result.native_metadata.append(
            make_sparse_map_row(
                extents,
                platform="macos",
                agent_id=request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID,
                complete=complete,
            )
        )
        merge_coverage(result.coverage, "storage_layout", "complete" if complete else "partial")
        if not complete:
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="storage_layout",
                    code="sparse_extent_limit_reached",
                    message="Sparse-map enumeration reached maximum_sparse_extents.",
                )
            )

    @staticmethod
    def _capture_special_features(
        stat: NativeStat, request: ObservationRequest, result: NativeCollection
    ) -> None:
        keys = (
            "generation",
            "document_id",
            "total_size",
            "allocation_size",
            "io_block_size",
            "data_length",
            "data_allocation_size",
            "resource_fork_length",
            "resource_fork_allocation_size",
        )
        data = {key: stat.extras[key] for key in keys if stat.extras.get(key) is not None}
        if not data:
            merge_coverage(result.coverage, "special_file_features", "not_supported")
            return
        result.native_metadata.append(
            {
                "kind": "native_stat_field",
                "coverage_category": "special_file_features",
                "namespace": "darwin.attrlist",
                "name": "file-fork-and-generation-attributes",
                "capture_status": "captured",
                "source": source("macos", "fgetattrlist(2)"),
                "value": {
                    "type": "json",
                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/darwin-file-attributes.json",
                    "data": data,
                },
                "sensitivity": "public",
            }
        )
        merge_coverage(result.coverage, "special_file_features", "complete")

    @staticmethod
    def _capture_native_stat(
        stat: NativeStat, request: ObservationRequest, result: NativeCollection
    ) -> None:
        data: dict[str, Any] = {
            key: value
            for key, value in {
                "device": stat.device,
                "inode": stat.inode,
                "mode": f"{stat.mode:o}",
                "nlink": stat.nlink,
                "uid": stat.uid,
                "gid": stat.gid,
                "size": stat.size,
                "blocks_512_bytes": stat.blocks,
                "preferred_io_block_size": stat.block_size,
                "flags": stat.flags,
                "generation": stat.generation,
                "rdev": stat.rdev,
            }.items()
            if value is not None
        }
        result.native_metadata.append(
            {
                "kind": "native_stat_field",
                "coverage_category": "native_metadata_other",
                "namespace": "darwin.stat",
                "name": "regular-file-stat",
                "capture_status": "captured",
                "source": source("macos", "fstat(2)/fgetattrlist(2)"),
                "value": {
                    "type": "json",
                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/darwin-file-stat.json",
                    "data": data,
                },
                "sensitivity": "public",
            }
        )

    def _environment(
        self,
        fs_info: DarwinFileSystemInfo | None,
        volume_attrs: dict[str, Any],
        request: ObservationRequest,
    ) -> JsonObject:
        product_version = (
            self.api.sysctl_text("kern.osproductversion")
            if hasattr(self.native, "sysctl_text")
            else None
        )
        build = (
            self.api.sysctl_text("kern.osversion") if hasattr(self.native, "sysctl_text") else None
        )
        hardware_model = (
            self.api.sysctl_text("hw.model") if hasattr(self.api, "sysctl_text") else None
        )
        mac_version = platform.mac_ver()[0]
        host: JsonObject = {
            "id": request.host_id,
            "hardware_architecture": platform.machine() or "unknown",
        }
        if request.policy.include_hostname:
            host["name"] = socket.gethostname()
        if hardware_model:
            host["hardware_model"] = hardware_model
        os_info: JsonObject = {
            "family": "macos",
            "name": "macOS",
            "version": product_version or mac_version or platform.release(),
            "kernel": {
                "name": platform.system() or "Darwin",
                "release": platform.release(),
                "version": platform.version(),
            },
        }
        if build:
            os_info["build"] = build
        filesystem: JsonObject = {
            "type": (fs_info.fs_type or "unknown") if fs_info else "unknown",
            "name_normalization": "implementation_defined",
        }
        if fs_info is not None:
            filesystem["mount_locator"] = locator_from_path(
                fs_info.mount_point,
                kind="absolute",
                authority_id=request.host_id,
            )
            filesystem["networked"] = fs_info.fs_type.lower() in _NETWORK_FILESYSTEMS
            volume_ids = [
                identifier(
                    scheme="darwin-fsid",
                    value=f"{fs_info.fsid[0]}:{fs_info.fsid[1]}",
                    scope="host",
                    authority_id=request.host_id,
                )
            ]
            mounted_from = portable_text_from_bytes(fs_info.mounted_from)
            if mounted_from:
                volume_ids.append(
                    identifier(
                        scheme="darwin-mounted-from",
                        value=mounted_from,
                        scope="host",
                        authority_id=request.host_id,
                    )
                )
            if volume_attrs.get("uuid"):
                volume_ids.append(
                    identifier(
                        scheme="volume-uuid",
                        value=volume_attrs["uuid"],
                        scope="global",
                    )
                )
            filesystem["volume_identifiers"] = volume_ids
        capabilities = volume_attrs.get("capabilities")
        valid = volume_attrs.get("valid_capabilities")
        if isinstance(capabilities, list) and isinstance(valid, list) and capabilities and valid:
            format_bits = capabilities[0]
            valid_format = valid[0]
            if valid_format & _VOL_CAP_FMT_CASE_SENSITIVE:
                filesystem["case_sensitive"] = bool(format_bits & _VOL_CAP_FMT_CASE_SENSITIVE)
            if valid_format & _VOL_CAP_FMT_CASE_PRESERVING:
                filesystem["case_preserving"] = bool(format_bits & _VOL_CAP_FMT_CASE_PRESERVING)
        return {
            "id": "urn:uuid:00000000-0000-0000-0000-000000000000",
            "type": "technical_environment",
            "host": host,
            "operating_system": os_info,
            "filesystem": filesystem,
            "runtime": runtime_environment(
                request.host_id,
                include_principal=request.policy.include_effective_principal,
            ),
        }


class MacOSFileStateObserver(DescriptorFileStateObserver):
    """Archive-level Riverhog provenance observer for macOS Tahoe 26.6 and later 26.x."""

    def __init__(
        self,
        *,
        native: MacOSNativeAPI | Any | None = None,
        enforce_platform: bool = True,
    ) -> None:
        super().__init__(MacOSBackend(native=native, enforce_platform=enforce_platform))
