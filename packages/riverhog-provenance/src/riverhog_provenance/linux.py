from __future__ import annotations

import ctypes
import ctypes.util
import errno

try:
    import fcntl
except ImportError:  # Importability on Windows; the backend rejects non-Linux use.
    fcntl = None  # type: ignore[assignment]
import os
import platform
import socket
import struct
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    DescriptorFileStateObserver,
    basic_access,
    diagnostic,
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
from .constants import DEFAULT_OBSERVER_AGENT_ID, OBSERVER_NAMESPACE
from .errors import (
    NativeObservationError,
    SymlinkRefusedError,
    UnsupportedPlatformError,
)
from .interface import PlatformBackend
from .model import ExtensionDraft, JsonObject, NativeCollection, NativeStat, ObservationRequest

# Linux uapi constants. These values are stable ABI, not libc implementation details.
AT_EMPTY_PATH = 0x1000
AT_STATX_SYNC_AS_STAT = 0x0000
STATX_TYPE = 0x00000001
STATX_MODE = 0x00000002
STATX_NLINK = 0x00000004
STATX_UID = 0x00000008
STATX_GID = 0x00000010
STATX_ATIME = 0x00000020
STATX_MTIME = 0x00000040
STATX_CTIME = 0x00000080
STATX_INO = 0x00000100
STATX_SIZE = 0x00000200
STATX_BLOCKS = 0x00000400
STATX_BASIC_STATS = 0x000007FF
STATX_BTIME = 0x00000800
STATX_MNT_ID = 0x00001000
STATX_DIOALIGN = 0x00002000
STATX_SUBVOL = 0x00008000
STATX_WRITE_ATOMIC = 0x00010000
STATX_DIO_READ_ALIGN = 0x00020000
STATX_ALL_WANTED = (
    STATX_BASIC_STATS
    | STATX_BTIME
    | STATX_MNT_ID
    | STATX_DIOALIGN
    | STATX_SUBVOL
    | STATX_WRITE_ATOMIC
    | STATX_DIO_READ_ALIGN
)

STATX_ATTR_COMPRESSED = 0x00000004
STATX_ATTR_IMMUTABLE = 0x00000010
STATX_ATTR_APPEND = 0x00000020
STATX_ATTR_NODUMP = 0x00000040
STATX_ATTR_ENCRYPTED = 0x00000800
STATX_ATTR_AUTOMOUNT = 0x00001000
STATX_ATTR_MOUNT_ROOT = 0x00002000
STATX_ATTR_VERITY = 0x00100000
STATX_ATTR_DAX = 0x00200000
STATX_ATTR_WRITE_ATOMIC = 0x00400000

_STATX_ATTRIBUTE_NAMES = {
    STATX_ATTR_COMPRESSED: "compressed",
    STATX_ATTR_IMMUTABLE: "immutable",
    STATX_ATTR_APPEND: "append_only",
    STATX_ATTR_NODUMP: "nodump",
    STATX_ATTR_ENCRYPTED: "encrypted",
    STATX_ATTR_AUTOMOUNT: "automount",
    STATX_ATTR_MOUNT_ROOT: "mount_root",
    STATX_ATTR_VERITY: "fs_verity",
    STATX_ATTR_DAX: "dax",
    STATX_ATTR_WRITE_ATOMIC: "atomic_write",
}

_FS_FLAG_NAMES = {
    0x00000001: "secure_deletion",
    0x00000002: "undelete",
    0x00000004: "compressed",
    0x00000008: "synchronous_updates",
    0x00000010: "immutable",
    0x00000020: "append_only",
    0x00000040: "nodump",
    0x00000080: "noatime",
    0x00000200: "compressed_blocks",
    0x00000400: "no_compression",
    0x00000800: "encrypted",
    0x00004000: "journal_data",
    0x00008000: "no_tail_merging",
    0x00010000: "directory_sync",
    0x00020000: "top_directory",
    0x00080000: "extents",
    0x00200000: "ea_inode",
    0x00800000: "nocow",
    0x02000000: "dax",
    0x10000000: "inline_data",
    0x20000000: "project_inherit",
    0x40000000: "casefold",
}

_FS_XFLAG_NAMES = {
    0x00000001: "realtime",
    0x00000002: "preallocated",
    0x00000008: "immutable",
    0x00000010: "append_only",
    0x00000020: "synchronous",
    0x00000040: "noatime",
    0x00000080: "nodump",
    0x00000100: "realtime_inherit",
    0x00000200: "project_inherit",
    0x00000400: "nosymlinks",
    0x00000800: "extent_size_inherit",
    0x00001000: "nodefrag",
    0x00002000: "filestream",
    0x00004000: "dax",
    0x00008000: "cow_extent_size",
    0x80000000: "has_attributes",
}

_NETWORK_FILESYSTEMS = {
    "9p",
    "afs",
    "cifs",
    "ceph",
    "davfs",
    "fuse.sshfs",
    "gfs2",
    "glusterfs",
    "lustre",
    "ncpfs",
    "nfs",
    "nfs4",
    "ocfs2",
    "smb3",
}


class _StatxTimestamp(ctypes.Structure):
    _fields_ = [
        ("tv_sec", ctypes.c_int64),
        ("tv_nsec", ctypes.c_uint32),
        ("__reserved", ctypes.c_int32),
    ]


class _Statx(ctypes.Structure):
    _fields_ = [
        ("stx_mask", ctypes.c_uint32),
        ("stx_blksize", ctypes.c_uint32),
        ("stx_attributes", ctypes.c_uint64),
        ("stx_nlink", ctypes.c_uint32),
        ("stx_uid", ctypes.c_uint32),
        ("stx_gid", ctypes.c_uint32),
        ("stx_mode", ctypes.c_uint16),
        ("__spare0", ctypes.c_uint16),
        ("stx_ino", ctypes.c_uint64),
        ("stx_size", ctypes.c_uint64),
        ("stx_blocks", ctypes.c_uint64),
        ("stx_attributes_mask", ctypes.c_uint64),
        ("stx_atime", _StatxTimestamp),
        ("stx_btime", _StatxTimestamp),
        ("stx_ctime", _StatxTimestamp),
        ("stx_mtime", _StatxTimestamp),
        ("stx_rdev_major", ctypes.c_uint32),
        ("stx_rdev_minor", ctypes.c_uint32),
        ("stx_dev_major", ctypes.c_uint32),
        ("stx_dev_minor", ctypes.c_uint32),
        ("stx_mnt_id", ctypes.c_uint64),
        ("stx_dio_mem_align", ctypes.c_uint32),
        ("stx_dio_offset_align", ctypes.c_uint32),
        ("stx_subvol", ctypes.c_uint64),
        ("stx_atomic_write_unit_min", ctypes.c_uint32),
        ("stx_atomic_write_unit_max", ctypes.c_uint32),
        ("stx_atomic_write_segments_max", ctypes.c_uint32),
        ("stx_dio_read_offset_align", ctypes.c_uint32),
        ("stx_atomic_write_unit_max_opt", ctypes.c_uint32),
        ("__spare2", ctypes.c_uint32),
        ("__spare3", ctypes.c_uint64 * 8),
    ]


if ctypes.sizeof(_Statx) != 256:  # pragma: no cover - architecture guard
    raise RuntimeError(f"unexpected Linux statx structure size: {ctypes.sizeof(_Statx)}")


@dataclass(frozen=True, slots=True)
class MountInfo:
    mount_id: int
    parent_id: int
    device: str
    root: str
    mount_point: str
    mount_options: tuple[str, ...]
    optional_fields: tuple[str, ...]
    fs_type: str
    source: str
    super_options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ACLCapture:
    raw: bytes
    text: str | None


class LinuxNativeAPI:
    """Small ctypes boundary around Linux APIs absent from Python's stdlib."""

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self._configure_libc()
        self.libacl = self._load_libacl()

    def _configure_libc(self) -> None:
        if hasattr(self.libc, "statx"):
            self.libc.statx.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.POINTER(_Statx),
            ]
            self.libc.statx.restype = ctypes.c_int
        self.libc.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        self.libc.flistxattr.restype = ctypes.c_ssize_t
        self.libc.fgetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.libc.fgetxattr.restype = ctypes.c_ssize_t

    @staticmethod
    def _load_libacl() -> ctypes.CDLL | None:
        name = ctypes.util.find_library("acl")
        if not name:
            return None
        library = ctypes.CDLL(name, use_errno=True)
        library.acl_get_fd.argtypes = [ctypes.c_int]
        library.acl_get_fd.restype = ctypes.c_void_p
        library.acl_size.argtypes = [ctypes.c_void_p]
        library.acl_size.restype = ctypes.c_ssize_t
        library.acl_copy_ext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
        library.acl_copy_ext.restype = ctypes.c_ssize_t
        library.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        library.acl_to_text.restype = ctypes.c_void_p
        library.acl_free.argtypes = [ctypes.c_void_p]
        library.acl_free.restype = ctypes.c_int
        return library

    def statx_fd(self, fd: int) -> _Statx | None:
        if not hasattr(self.libc, "statx"):
            return None
        # Retry with progressively older request masks.  Older kernels may
        # reject mask bits introduced after the syscall itself, while the
        # fixed 256-byte statx ABI remains compatible.
        masks = (
            STATX_ALL_WANTED,
            STATX_BASIC_STATS | STATX_BTIME | STATX_MNT_ID | STATX_DIOALIGN,
            STATX_BASIC_STATS | STATX_BTIME | STATX_MNT_ID,
            STATX_BASIC_STATS | STATX_BTIME,
        )
        for mask in masks:
            result = _Statx()
            ctypes.set_errno(0)
            rc = self.libc.statx(
                fd,
                b"",
                AT_EMPTY_PATH | AT_STATX_SYNC_AS_STAT,
                mask,
                ctypes.byref(result),
            )
            if rc == 0:
                return result
            error = ctypes.get_errno()
            if error == errno.EINVAL:
                continue
            if error in {errno.ENOSYS, errno.EOPNOTSUPP}:
                return None
            raise OSError(error, os.strerror(error))
        return None

    def list_xattrs(self, fd: int) -> list[bytes]:
        for _ in range(4):
            ctypes.set_errno(0)
            required = self.libc.flistxattr(fd, None, 0)
            if required < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            if required == 0:
                return []
            buffer = ctypes.create_string_buffer(required)
            actual = self.libc.flistxattr(fd, buffer, required)
            if actual >= 0:
                return [part for part in bytes(buffer.raw[:actual]).split(b"\0") if part]
            error = ctypes.get_errno()
            if error != errno.ERANGE:
                raise OSError(error, os.strerror(error))
        raise OSError(errno.ERANGE, "xattr name list changed repeatedly")

    def get_xattr(self, fd: int, name: bytes, maximum: int) -> tuple[int, bytes | None]:
        for _ in range(4):
            ctypes.set_errno(0)
            required = self.libc.fgetxattr(fd, name, None, 0)
            if required < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            if required > maximum:
                return required, None
            if required == 0:
                return 0, b""
            buffer = ctypes.create_string_buffer(required)
            actual = self.libc.fgetxattr(fd, name, buffer, required)
            if actual >= 0:
                return actual, bytes(buffer.raw[:actual])
            error = ctypes.get_errno()
            if error != errno.ERANGE:
                raise OSError(error, os.strerror(error))
        raise OSError(errno.ERANGE, f"xattr {name!r} changed repeatedly")

    def get_acl(self, fd: int) -> ACLCapture | None:
        if self.libacl is None:
            return None
        ctypes.set_errno(0)
        acl = self.libacl.acl_get_fd(fd)
        if not acl:
            error = ctypes.get_errno()
            if error in {errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)}:
                return ACLCapture(raw=b"", text=None)
            raise OSError(error, os.strerror(error))
        try:
            size = self.libacl.acl_size(acl)
            if size < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            raw_buffer = ctypes.create_string_buffer(size)
            copied = self.libacl.acl_copy_ext(raw_buffer, acl, size)
            if copied < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            # libacl implementations return either zero or the copied byte
            # count on success.  acl_size(3) is the authoritative required
            # external-representation size in the zero-return form.
            raw_length = size if copied == 0 else copied
            if raw_length > size:
                raise OSError(errno.EOVERFLOW, "ACL external form exceeded acl_size")
            raw = bytes(raw_buffer.raw[:raw_length])
            text_length = ctypes.c_ssize_t()
            text_pointer = self.libacl.acl_to_text(acl, ctypes.byref(text_length))
            text: str | None = None
            if text_pointer:
                try:
                    text_bytes = ctypes.string_at(text_pointer, max(0, text_length.value))
                    text = text_bytes.decode("utf-8", "replace")
                finally:
                    self.libacl.acl_free(text_pointer)
            return ACLCapture(raw=raw, text=text)
        finally:
            self.libacl.acl_free(acl)


def _timespec_ns(value: _StatxTimestamp) -> int:
    return int(value.tv_sec) * 1_000_000_000 + int(value.tv_nsec)


def _decode_mount_field(value: str) -> str:
    # proc(5) mountinfo octal escaping is deliberately not shell escaping.
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def _portable_mount_field(value: str) -> str:
    """Return JSON/PostgreSQL-safe text without losing non-UTF-8 mount bytes."""

    return portable_text_from_bytes(os.fsencode(value))


def read_mountinfo() -> list[MountInfo]:
    records: list[MountInfo] = []
    try:
        lines = (
            Path("/proc/self/mountinfo")
            .read_text(encoding="utf-8", errors="surrogateescape")
            .splitlines()
        )
    except OSError:
        return records
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            before = fields[:separator]
            after = fields[separator + 1 :]
            records.append(
                MountInfo(
                    mount_id=int(before[0]),
                    parent_id=int(before[1]),
                    device=before[2],
                    root=_decode_mount_field(before[3]),
                    mount_point=_decode_mount_field(before[4]),
                    mount_options=tuple(before[5].split(",")),
                    optional_fields=tuple(before[6:]),
                    fs_type=after[0],
                    source=_decode_mount_field(after[1]),
                    super_options=tuple(after[2].split(",")) if len(after) > 2 else (),
                )
            )
        except (ValueError, IndexError):
            continue
    return records


def _find_mount(stat: NativeStat, path: str | bytes) -> MountInfo | None:
    records = read_mountinfo()
    if stat.mount_id is not None:
        for record in records:
            if record.mount_id == stat.mount_id:
                return record
    decoded = os.fsdecode(path)
    candidates = [
        record
        for record in records
        if decoded == record.mount_point or decoded.startswith(record.mount_point.rstrip("/") + "/")
    ]
    return max(candidates, key=lambda item: len(item.mount_point), default=None)


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for candidate in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if value.startswith(('"', "'")) and value.endswith(value[0]):
                value = value[1:-1]
            values[key] = value
        break
    return values


def _ioc(direction: int, type_: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (type_ << 8) | number


def _ior(type_char: str, number: int, size: int) -> int:
    return _ioc(2, ord(type_char), number, size)


FS_IOC_GETFLAGS = _ior("f", 1, ctypes.sizeof(ctypes.c_long))
FSXATTR_STRUCT_SIZE = struct.calcsize("=IIIII8s")
FS_IOC_FSGETXATTR = _ior("X", 31, FSXATTR_STRUCT_SIZE)


def _names_from_mask(value: int, mapping: dict[int, str]) -> list[str]:
    return [name for bit, name in mapping.items() if value & bit]


def _classify_xattr(name: bytes) -> tuple[str, str, str, str]:
    text = name.decode("utf-8", "replace")
    namespace = text.split(".", 1)[0] if "." in text else "linux"
    if name in {b"system.posix_acl_access", b"system.posix_acl_default", b"system.nfs4_acl"}:
        return "acl", "access_control", namespace, "security_sensitive"
    if name == b"security.capability":
        return "capability", "security_metadata", namespace, "security_sensitive"
    if name.startswith(b"security."):
        return "security_label", "security_metadata", namespace, "security_sensitive"
    if name.startswith(b"trusted."):
        return "extended_attribute", "security_metadata", namespace, "security_sensitive"
    sensitivity = "personal" if name.startswith(b"user.") else "unknown"
    return "extended_attribute", "extended_attributes", namespace, sensitivity


class UbuntuBackend(PlatformBackend):
    platform_family = "linux"

    def __init__(
        self,
        *,
        native: LinuxNativeAPI | None = None,
        enforce_platform: bool = True,
        enforce_ubuntu: bool = True,
    ) -> None:
        self.native = native
        self.enforce_platform = enforce_platform
        self.enforce_ubuntu = enforce_ubuntu

    def assert_supported(self) -> None:
        if self.enforce_platform and not sys.platform.startswith("linux"):
            raise UnsupportedPlatformError("Ubuntu observer requires Linux")
        if self.enforce_ubuntu:
            release = _read_os_release()
            if release.get("ID", "").lower() != "ubuntu":
                raise UnsupportedPlatformError(
                    "Ubuntu observer requires an Ubuntu userspace; set "
                    "enforce_ubuntu=False only for compatible test hosts"
                )
        if self.native is None:
            self.native = LinuxNativeAPI()

    @property
    def api(self) -> LinuxNativeAPI:
        if self.native is None:
            raise RuntimeError("Linux native API is unavailable before platform validation")
        return self.native

    def open_readonly(
        self, path: str | bytes, request: ObservationRequest
    ) -> tuple[int, list[JsonObject], bool]:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        # Avoid blocking if an adversarial or concurrent path replacement
        # swaps the prechecked regular file for a FIFO or device before open.
        flags |= getattr(os, "O_NONBLOCK", 0)
        diagnostics: list[JsonObject] = []
        noatime = False
        noatime_flag = getattr(os, "O_NOATIME", 0)
        if request.policy.attempt_noatime and noatime_flag:
            try:
                return os.open(path, flags | noatime_flag), diagnostics, True
            except OSError as exc:
                if exc.errno not in {errno.EPERM, errno.EACCES, errno.EINVAL, errno.EOPNOTSUPP}:
                    if exc.errno == errno.ELOOP:
                        raise SymlinkRefusedError("final path component is a symlink") from exc
                    raise
                diagnostics.append(
                    diagnostic(
                        severity="warning",
                        category="timestamps",
                        code="o_noatime_unavailable",
                        message="O_NOATIME could not be used; capture retried without it.",
                        native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                        source_descriptor=source("linux", "open(2)", "O_NOATIME"),
                    )
                )
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SymlinkRefusedError("final path component is a symlink") from exc
            raise
        return fd, diagnostics, noatime

    def stat_fd(self, fd: int) -> NativeStat:
        statx = self.api.statx_fd(fd)
        fallback = os.fstat(fd)
        if statx is None:
            return NativeStat(
                device=fallback.st_dev,
                inode=fallback.st_ino,
                mode=fallback.st_mode,
                nlink=fallback.st_nlink,
                uid=fallback.st_uid,
                gid=fallback.st_gid,
                size=fallback.st_size,
                atime_ns=fallback.st_atime_ns,
                mtime_ns=fallback.st_mtime_ns,
                ctime_ns=fallback.st_ctime_ns,
                blocks=getattr(fallback, "st_blocks", None),
                block_size=getattr(fallback, "st_blksize", None),
                rdev=fallback.st_rdev,
                extras={"statx_available": False},
            )

        mask = int(statx.stx_mask)

        def selected(bits: int, value: int, fallback_value: int) -> int:
            return int(value) if (mask & bits) == bits else int(fallback_value)

        device = os.makedev(statx.stx_dev_major, statx.stx_dev_minor)
        rdev = os.makedev(statx.stx_rdev_major, statx.stx_rdev_minor)
        birthtime = _timespec_ns(statx.stx_btime) if mask & STATX_BTIME else None
        extras: dict[str, Any] = {
            "statx_available": True,
            "statx_mask": mask,
            "statx_attributes": int(statx.stx_attributes),
            "statx_attributes_mask": int(statx.stx_attributes_mask),
            "dev_major": int(statx.stx_dev_major),
            "dev_minor": int(statx.stx_dev_minor),
        }
        if mask & STATX_DIOALIGN:
            extras.update(
                dio_mem_align=int(statx.stx_dio_mem_align),
                dio_offset_align=int(statx.stx_dio_offset_align),
            )
        if mask & STATX_SUBVOL:
            extras["subvolume_id"] = int(statx.stx_subvol)
        if mask & STATX_WRITE_ATOMIC:
            extras.update(
                atomic_write_unit_min=int(statx.stx_atomic_write_unit_min),
                atomic_write_unit_max=int(statx.stx_atomic_write_unit_max),
                atomic_write_segments_max=int(statx.stx_atomic_write_segments_max),
                atomic_write_unit_max_opt=int(statx.stx_atomic_write_unit_max_opt),
            )
        if mask & STATX_DIO_READ_ALIGN:
            extras["dio_read_offset_align"] = int(statx.stx_dio_read_offset_align)

        return NativeStat(
            device=device,
            inode=selected(STATX_INO, statx.stx_ino, fallback.st_ino),
            mode=selected(STATX_TYPE | STATX_MODE, statx.stx_mode, fallback.st_mode),
            nlink=selected(STATX_NLINK, statx.stx_nlink, fallback.st_nlink),
            uid=selected(STATX_UID, statx.stx_uid, fallback.st_uid),
            gid=selected(STATX_GID, statx.stx_gid, fallback.st_gid),
            size=selected(STATX_SIZE, statx.stx_size, fallback.st_size),
            atime_ns=(
                _timespec_ns(statx.stx_atime) if mask & STATX_ATIME else fallback.st_atime_ns
            ),
            mtime_ns=(
                _timespec_ns(statx.stx_mtime) if mask & STATX_MTIME else fallback.st_mtime_ns
            ),
            ctime_ns=(
                _timespec_ns(statx.stx_ctime) if mask & STATX_CTIME else fallback.st_ctime_ns
            ),
            birthtime_ns=birthtime,
            blocks=selected(
                STATX_BLOCKS,
                statx.stx_blocks,
                getattr(fallback, "st_blocks", 0),
            ),
            block_size=int(statx.stx_blksize or getattr(fallback, "st_blksize", 0)),
            mount_id=(int(statx.stx_mnt_id) if mask & STATX_MNT_ID else None),
            rdev=rdev,
            extras=extras,
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
                "resource_forks": "not_applicable",
            }
        )
        mount = _find_mount(stat, path)
        volume_authority = self._volume_authority(request.host_id, stat, mount)
        result.native_identifiers = self._native_identifiers(
            stat, request.host_id, volume_authority
        )

        if request.policy.capture_xattrs:
            self._capture_xattrs(fd, request, result)
        else:
            result.coverage["extended_attributes"] = "not_requested"
            result.coverage["security_metadata"] = "not_requested"

        if request.policy.capture_acl:
            self._capture_acl(fd, request, result)
        else:
            result.coverage["access_control"] = "not_requested"

        if request.policy.capture_file_flags:
            self._capture_file_flags(fd, stat, request, result)
        else:
            result.coverage["file_flags"] = "not_requested"

        if request.policy.capture_sparse_map:
            self._capture_sparse_map(fd, stat, request, result)
        else:
            result.coverage["storage_layout"] = "not_requested"

        if request.policy.capture_special_features:
            self._capture_special_features(fd, request, result)
        else:
            result.coverage["special_file_features"] = "not_requested"

        if request.policy.capture_native_stat:
            self._capture_native_stat(stat, request, result)
            result.coverage["native_metadata_other"] = "complete"
        else:
            result.coverage["native_metadata_other"] = "not_requested"

        result.environment = self._environment(stat, path, mount, request)
        if mount is not None:
            result.extension_drafts.append(
                ExtensionDraft(
                    subject_role="environment",
                    property="https://nashspence.github.io/riverhog/v1/provenance/observers/vocab/linux-mount-context",
                    value={
                        "type": "json",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-mount-context.json",
                        "data": {
                            "mount_id": mount.mount_id,
                            "parent_id": mount.parent_id,
                            "device": mount.device,
                            "root": _portable_mount_field(mount.root),
                            "mount_point": _portable_mount_field(mount.mount_point),
                            "mount_options": [
                                _portable_mount_field(value) for value in mount.mount_options
                            ],
                            "optional_fields": [
                                _portable_mount_field(value) for value in mount.optional_fields
                            ],
                            "filesystem_type": _portable_mount_field(mount.fs_type),
                            "source": _portable_mount_field(mount.source),
                            "super_options": [
                                _portable_mount_field(value) for value in mount.super_options
                            ],
                        },
                    },
                    note="Raw /proc/self/mountinfo context for the observed descriptor mount.",
                )
            )
        return result

    @staticmethod
    def _timestamps(stat: NativeStat, request: ObservationRequest) -> list[JsonObject]:
        api = "statx(2)" if stat.extras.get("statx_available") else "fstat(2)"
        timestamps = [
            timestamp_observation(
                kind="content_modified",
                epoch_ns=stat.mtime_ns,
                platform="linux",
                api=api,
                field="stx_mtime" if api == "statx(2)" else "st_mtim",
            ),
            timestamp_observation(
                kind="metadata_changed",
                epoch_ns=stat.ctime_ns,
                platform="linux",
                api=api,
                field="stx_ctime" if api == "statx(2)" else "st_ctim",
            ),
        ]
        if request.policy.include_access_time:
            timestamps.append(
                timestamp_observation(
                    kind="accessed",
                    epoch_ns=stat.atime_ns,
                    platform="linux",
                    api=api,
                    field="stx_atime" if api == "statx(2)" else "st_atim",
                )
            )
        if stat.birthtime_ns is not None:
            timestamps.append(
                timestamp_observation(
                    kind="created",
                    epoch_ns=stat.birthtime_ns,
                    platform="linux",
                    api="statx(2)",
                    field="stx_btime",
                )
            )
        return timestamps

    @staticmethod
    def _volume_authority(host_id: str, stat: NativeStat, mount: MountInfo | None) -> str:
        key = f"linux-volume:{host_id}:{stat.device}:{mount.mount_id if mount else stat.mount_id}"
        return f"urn:uuid:{uuid.uuid5(OBSERVER_NAMESPACE, key)}"

    @staticmethod
    def _native_identifiers(
        stat: NativeStat, host_id: str, volume_authority: str
    ) -> list[JsonObject]:
        api = "statx(2)" if stat.extras.get("statx_available") else "fstat(2)"
        identifiers = [
            observed_identifier(
                scheme="linux-inode",
                value=str(stat.inode),
                scope="filesystem",
                authority_id=volume_authority,
                platform="linux",
                api=api,
                field="stx_ino" if api == "statx(2)" else "st_ino",
            ),
            observed_identifier(
                scheme="linux-device-number",
                value=f"{os.major(stat.device)}:{os.minor(stat.device)}",
                scope="host",
                authority_id=host_id,
                platform="linux",
                api=api,
                field="stx_dev_major:stx_dev_minor" if api == "statx(2)" else "st_dev",
            ),
        ]
        if stat.mount_id is not None:
            identifiers.append(
                observed_identifier(
                    scheme="linux-mount-id",
                    value=str(stat.mount_id),
                    scope="host",
                    authority_id=host_id,
                    platform="linux",
                    api="statx(2)",
                    field="stx_mnt_id",
                )
            )
        return identifiers

    def _capture_xattrs(
        self, fd: int, request: ObservationRequest, result: NativeCollection
    ) -> None:
        had_error: dict[str, bool] = {
            "extended_attributes": False,
            "security_metadata": False,
            "access_control": False,
        }
        seen: set[str] = set()
        try:
            names = self.api.list_xattrs(fd)
        except OSError as exc:
            result.coverage["extended_attributes"] = "failed"
            result.coverage["security_metadata"] = "failed"
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="extended_attributes",
                    code="flistxattr_failed",
                    message="Could not enumerate Linux extended attributes.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("linux", "flistxattr(2)"),
                )
            )
            return
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
                "source": source("linux", "fgetxattr(2)", fields["name"]),
                "sensitivity": sensitivity,
            }
            try:
                observed_length, data = self.api.get_xattr(
                    fd, name, request.policy.maximum_native_value_bytes
                )
                row["observed_byte_length"] = observed_length
                if data is None:
                    if request.policy.large_value_disposition.value == "fail":
                        raise NativeObservationError(
                            f"xattr {name!r} exceeds maximum_native_value_bytes"
                        )
                    row["capture_status"] = "not_retained"
                    row["note"] = (
                        "Value exceeded maximum_native_value_bytes; xattr APIs do not "
                        "provide a portable streaming read for this value."
                    )
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
                        source_descriptor=source("linux", "fgetxattr(2)", fields["name"]),
                    )
                )
            result.native_metadata.append(row)
        for category in ("extended_attributes", "security_metadata"):
            result.coverage[category] = "partial" if had_error[category] else "complete"
        if "access_control" in seen:
            # The xattr itself is source-native ACL evidence.  Preserve its result
            # even when libacl is absent, and never hide a failed xattr read behind
            # a successful interpretation through the dedicated ACL API.
            merge_coverage(
                result.coverage,
                "access_control",
                "partial" if had_error["access_control"] else "complete",
            )

    def _capture_acl(self, fd: int, request: ObservationRequest, result: NativeCollection) -> None:
        if self.api.libacl is None:
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
                    code="acl_get_fd_failed",
                    message="Could not read the POSIX access ACL.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("linux", "acl_get_fd(3)"),
                )
            )
            return
        merge_coverage(result.coverage, "access_control", "complete")
        if captured is None:
            return
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        status, value, note = retained_native_value(
            captured.raw, agent_id=agent_id, request=request
        )
        row: JsonObject = {
            "kind": "acl",
            "coverage_category": "access_control",
            "namespace": "posix.1e",
            "name": "access-acl",
            "capture_status": status,
            "source": source("linux", "acl_get_fd(3)", "ACL_TYPE_ACCESS"),
            "observed_byte_length": len(captured.raw),
            "sensitivity": "security_sensitive",
        }
        if value is not None:
            row["value"] = value
        if note:
            row["note"] = note
        if captured.text is not None:
            row["interpretations"] = [
                {
                    "kind": "text_decode",
                    "value": {
                        "type": "text",
                        "data": captured.text,
                        "source_encoding": "UTF-8",
                        "byte_length": len(captured.text.encode("utf-8")),
                        "media_type": "text/plain",
                    },
                    "agent_id": agent_id,
                    "confidence": "high",
                    "note": "Text generated by acl_to_text(3).",
                }
            ]
        result.native_metadata.append(row)

    def _capture_file_flags(
        self,
        fd: int,
        stat: NativeStat,
        request: ObservationRequest,
        result: NativeCollection,
    ) -> None:
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        captured_any = False
        ioctl_failed = False
        if stat.extras.get("statx_available"):
            attributes = int(stat.extras.get("statx_attributes", 0))
            mask = int(stat.extras.get("statx_attributes_mask", 0))
            data = {
                "attributes": attributes,
                "attributes_mask": mask,
                "set_names": _names_from_mask(attributes, _STATX_ATTRIBUTE_NAMES),
                "supported_names": _names_from_mask(mask, _STATX_ATTRIBUTE_NAMES),
            }
            result.native_metadata.append(
                {
                    "kind": "file_flag",
                    "coverage_category": "file_flags",
                    "namespace": "linux.statx",
                    "name": "stx_attributes",
                    "capture_status": "captured",
                    "source": source("linux", "statx(2)", "stx_attributes"),
                    "value": {
                        "type": "json",
                        "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-statx-attributes.json",
                        "data": data,
                    },
                    "interpretations": [
                        {
                            "kind": "structured_parse",
                            "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-statx-attributes.json",
                            "value": {
                                "type": "json",
                                "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-statx-attributes.json",
                                "data": data,
                            },
                            "agent_id": agent_id,
                            "confidence": "high",
                        }
                    ],
                    "sensitivity": "public",
                }
            )
            captured_any = True
        try:
            buffer = bytearray(ctypes.sizeof(ctypes.c_long))
            fcntl.ioctl(fd, FS_IOC_GETFLAGS, buffer, True)
            flags = int.from_bytes(buffer, byteorder=sys.byteorder, signed=False)
        except OSError as exc:
            if exc.errno not in {errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL}:
                ioctl_failed = True
                result.diagnostics.append(
                    diagnostic(
                        severity="error",
                        category="file_flags",
                        code="fs_ioc_getflags_failed",
                        message="FS_IOC_GETFLAGS failed.",
                        native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                        source_descriptor=source("linux", "ioctl(2)", "FS_IOC_GETFLAGS"),
                    )
                )
        else:
            data = {"raw": flags, "set_names": _names_from_mask(flags, _FS_FLAG_NAMES)}
            result.native_metadata.append(
                {
                    "kind": "file_flag",
                    "coverage_category": "file_flags",
                    "namespace": "linux.fs",
                    "name": "FS_IOC_GETFLAGS",
                    "capture_status": "captured",
                    "source": source("linux", "ioctl(2)", "FS_IOC_GETFLAGS"),
                    "value": {"type": "integer", "data": str(flags)},
                    "interpretations": [
                        {
                            "kind": "structured_parse",
                            "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-fs-flags.json",
                            "value": {
                                "type": "json",
                                "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-fs-flags.json",
                                "data": data,
                            },
                            "agent_id": agent_id,
                            "confidence": "high",
                        }
                    ],
                    "sensitivity": "public",
                }
            )
            captured_any = True
        if ioctl_failed:
            result.coverage["file_flags"] = "partial" if captured_any else "failed"
        else:
            result.coverage["file_flags"] = "complete" if captured_any else "not_supported"

    def _capture_sparse_map(
        self,
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
                result.coverage["storage_layout"] = "not_supported"
                return
            result.coverage["storage_layout"] = "failed"
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="storage_layout",
                    code="seek_data_hole_failed",
                    message="Could not enumerate sparse extents.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("linux", "lseek(2)", "SEEK_DATA/SEEK_HOLE"),
                )
            )
            return
        agent_id = request.observer_agent_id or DEFAULT_OBSERVER_AGENT_ID
        result.native_metadata.append(
            make_sparse_map_row(
                extents,
                platform="linux",
                agent_id=agent_id,
                complete=complete,
            )
        )
        result.coverage["storage_layout"] = "complete" if complete else "partial"
        if not complete:
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="storage_layout",
                    code="sparse_extent_limit_reached",
                    message="Sparse-map enumeration reached maximum_sparse_extents.",
                )
            )

    def _capture_special_features(
        self, fd: int, request: ObservationRequest, result: NativeCollection
    ) -> None:
        try:
            buffer = bytearray(FSXATTR_STRUCT_SIZE)
            fcntl.ioctl(fd, FS_IOC_FSGETXATTR, buffer, True)
        except OSError as exc:
            if exc.errno in {errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL}:
                result.coverage["special_file_features"] = "not_supported"
                return
            result.coverage["special_file_features"] = "failed"
            result.diagnostics.append(
                diagnostic(
                    severity="error",
                    category="special_file_features",
                    code="fs_ioc_fsgetxattr_failed",
                    message="FS_IOC_FSGETXATTR failed.",
                    native_code=errno.errorcode.get(exc.errno, str(exc.errno)),
                    source_descriptor=source("linux", "ioctl(2)", "FS_IOC_FSGETXATTR"),
                )
            )
            return
        xflags, extsize, nextents, projid, cowextsize = struct.unpack_from("=IIIII", buffer)
        data = {
            "xflags": xflags,
            "xflag_names": _names_from_mask(xflags, _FS_XFLAG_NAMES),
            "extent_size": extsize,
            "nextents": nextents,
            "project_id": projid,
            "cow_extent_size": cowextsize,
        }
        result.native_metadata.append(
            {
                "kind": "native_stat_field",
                "coverage_category": "special_file_features",
                "namespace": "linux.fsxattr",
                "name": "FS_IOC_FSGETXATTR",
                "capture_status": "captured",
                "source": source("linux", "ioctl(2)", "FS_IOC_FSGETXATTR"),
                "value": {
                    "type": "json",
                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-fsxattr.json",
                    "data": data,
                },
                "sensitivity": "public",
            }
        )
        result.coverage["special_file_features"] = "complete"

    @staticmethod
    def _capture_native_stat(
        stat: NativeStat, request: ObservationRequest, result: NativeCollection
    ) -> None:
        api = "statx(2)" if stat.extras.get("statx_available") else "fstat(2)"
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
                "rdev": stat.rdev,
            }.items()
            if value is not None
        }
        data.update({key: value for key, value in stat.extras.items() if value is not None})
        result.native_metadata.append(
            {
                "kind": "native_stat_field",
                "coverage_category": "native_metadata_other",
                "namespace": "linux.statx" if api == "statx(2)" else "posix.stat",
                "name": "regular-file-stat",
                "capture_status": "captured",
                "source": source("linux", api),
                "value": {
                    "type": "json",
                    "schema": "https://nashspence.github.io/riverhog/v1/provenance/observers/schemas/linux-file-stat.json",
                    "data": data,
                },
                "sensitivity": "public",
            }
        )

    @staticmethod
    def _environment(
        stat: NativeStat,
        path: str | bytes,
        mount: MountInfo | None,
        request: ObservationRequest,
    ) -> JsonObject:
        release = _read_os_release()
        host: JsonObject = {
            "id": request.host_id,
            "hardware_architecture": platform.machine() or os.uname().machine,
        }
        if request.policy.include_hostname:
            host["name"] = socket.gethostname()
        os_info: JsonObject = {
            "family": "linux",
            "name": release.get("PRETTY_NAME") or release.get("NAME") or "Linux",
            "version": release.get("VERSION_ID") or platform.release(),
            "kernel": {
                "name": platform.system() or "Linux",
                "release": platform.release(),
                "version": platform.version(),
            },
        }
        if release.get("BUILD_ID"):
            os_info["build"] = release["BUILD_ID"]
        identifiers: list[JsonObject] = []
        if release.get("ID"):
            identifiers.append(
                identifier(
                    scheme="os-release-id",
                    value=release["ID"],
                    scope="global",
                )
            )
        if release.get("VERSION_ID"):
            identifiers.append(
                identifier(
                    scheme="os-release-version-id",
                    value=release["VERSION_ID"],
                    scope="global",
                )
            )
        if identifiers:
            os_info["identifiers"] = identifiers

        filesystem: JsonObject = {
            "type": _portable_mount_field(mount.fs_type) if mount else "unknown",
            "name_normalization": "unknown",
        }
        if mount is not None:
            filesystem["mount_locator"] = locator_from_path(
                os.fsencode(mount.mount_point),
                kind="absolute",
                authority_id=request.host_id,
            )
            filesystem["networked"] = mount.fs_type.lower() in _NETWORK_FILESYSTEMS
            volume_identifiers = [
                identifier(
                    scheme="linux-mount-id",
                    value=str(mount.mount_id),
                    scope="host",
                    authority_id=request.host_id,
                ),
                identifier(
                    scheme="linux-device-major-minor",
                    value=mount.device,
                    scope="host",
                    authority_id=request.host_id,
                ),
            ]
            if mount.source and mount.source != "none":
                volume_identifiers.append(
                    identifier(
                        scheme="linux-mount-source",
                        value=_portable_mount_field(mount.source),
                        scope="host",
                        authority_id=request.host_id,
                    )
                )
            filesystem["volume_identifiers"] = volume_identifiers
        return {
            "id": "urn:uuid:00000000-0000-0000-0000-000000000000",  # replaced by engine
            "type": "technical_environment",
            "host": host,
            "operating_system": os_info,
            "filesystem": filesystem,
            "runtime": runtime_environment(
                request.host_id,
                include_principal=request.policy.include_effective_principal,
            ),
        }


class UbuntuFileStateObserver(DescriptorFileStateObserver):
    """Archive-level Riverhog provenance observer for Ubuntu 26.04 LTS and compatible Linux."""

    def __init__(
        self,
        *,
        native: LinuxNativeAPI | None = None,
        enforce_platform: bool = True,
        enforce_ubuntu: bool = True,
    ) -> None:
        super().__init__(
            UbuntuBackend(
                native=native,
                enforce_platform=enforce_platform,
                enforce_ubuntu=enforce_ubuntu,
            )
        )
