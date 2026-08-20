from __future__ import annotations

import base64
import errno
import hashlib
import os
import sys
from pathlib import Path

import pytest
from riverhog_provenance import (
    LinuxFileStateObserver,
    ObservationPolicy,
    ObservationRequest,
    PayloadBindingRequest,
    SymlinkRefusedError,
    UnstableFileError,
    prepare_file_provenance,
    validate_graph_fragment,
)
from riverhog_provenance.linux import (
    FS_IOC_FSGETXATTR,
    FSXATTR_STRUCT_SIZE,
    LinuxBackend,
    _portable_mount_field,
)
from riverhog_provenance.model import NativeCollection, NativeStat

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


def _observer() -> LinuxFileStateObserver:
    return LinuxFileStateObserver()


def test_live_linux_observation_is_riverhog_provenance_valid(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "payload.bin"
    content = b"opaque primary bytes\x00\xff\n"
    payload.write_bytes(content)
    payload.chmod(0o644)
    try:
        os.setxattr(payload, b"user.riverhog-provenance-test", b"native-value")
    except OSError:
        pass

    result = _observer().observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=urn_factory(),
            payload_binding=PayloadBindingRequest(),
            policy=ObservationPolicy(second_content_hash=True),
        )
    )
    fragment = result.graph_fragment()
    validate_graph_fragment(fragment)

    state = result.state
    assert state["content"]["size_bytes"] == len(content)
    assert state["content"]["digests"][0]["value"] == hashlib.sha256(content).hexdigest()
    assert state["filesystem_metadata"]["access"]["posix_mode"] == "0644"
    assert result.capture["consistency"] == "verified_unchanged"
    assert result.capture["outcome"] == "success"
    assert result.capture["coverage"]["content_fixity"] == "complete"
    assert result.capture["coverage"]["basic_filesystem"] == "complete"
    assert result.payload_binding is not None


def test_auto_linux_observation_uses_the_linux_abi_across_distributions(
    tmp_path: Path, monkeypatch
) -> None:
    import riverhog_provenance.linux as linux_module

    payload = tmp_path / "container-payload"
    payload.write_bytes(b"containerized Stove0 custody")
    monkeypatch.setattr(
        linux_module,
        "_read_os_release",
        lambda: {"ID": "debian", "NAME": "Debian GNU/Linux"},
    )

    prepared = prepare_file_provenance(
        payload,
        relative_path="container-payload",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
        agent_name="stove0-server",
        agent_version="1.0.0",
    )

    assert prepared.source == "captured"
    assert prepared.binding.status == "captured"
    assert prepared.binding.bytes == len(b"containerized Stove0 custody")


def test_linux_non_utf8_filename_round_trips(tmp_path: Path, urn_factory) -> None:
    directory = os.fsencode(tmp_path)
    path = directory + b"/name-\xff.bin"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"data")
    finally:
        os.close(fd)
    result = _observer().observe(
        ObservationRequest(path=path, lineage_id=urn_factory(), host_id=urn_factory())
    )
    locator = result.state["locator"]
    assert locator["text_role"] == "display"
    assert base64.b64decode(locator["bytes"]["data"]) == os.path.abspath(path)
    validate_graph_fragment(result.graph_fragment())


def test_symlink_final_component_is_refused(tmp_path: Path, urn_factory) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SymlinkRefusedError):
        _observer().observe(
            ObservationRequest(path=link, lineage_id=urn_factory(), host_id=urn_factory())
        )


def test_replacement_binding_emits_unbind_and_bind(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "state.dat"
    payload.write_bytes(b"state")
    old_binding = urn_factory()
    result = _observer().observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=urn_factory(),
            payload_binding=PayloadBindingRequest(replaces_binding_id=old_binding),
        )
    )
    assert [item["operation"] for item in result.payload_bindings] == ["unbind", "bind"]
    unbind, bind = result.payload_bindings
    assert unbind["replaces_binding_id"] == old_binding
    assert bind["replaces_binding_id"] == unbind["id"]
    validate_graph_fragment(result.graph_fragment())


def test_regular_file_only(tmp_path: Path, urn_factory) -> None:
    from riverhog_provenance import UnsupportedFileTypeError

    with pytest.raises(UnsupportedFileTypeError):
        _observer().observe(
            ObservationRequest(path=tmp_path, lineage_id=urn_factory(), host_id=urn_factory())
        )


def test_locator_authority_is_environment_host_entity(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "authority.dat"
    payload.write_bytes(b"authority")
    stable_host_authority = urn_factory()
    result = _observer().observe(
        ObservationRequest(
            path=payload,
            lineage_id=urn_factory(),
            host_id=stable_host_authority,
        )
    )
    host = result.environment["host"]
    assert result.state["locator"]["authority_id"] == host["id"]
    mount_locator = result.environment["filesystem"].get("mount_locator")
    if mount_locator is not None:
        assert mount_locator["authority_id"] == host["id"]
    assert host["id"] != stable_host_authority
    assert {
        item["value"] for item in host["identifiers"] if item["scheme"] == "riverhog-host-authority"
    } == {stable_host_authority}


def test_fsgetxattr_ioctl_uses_exact_linux_fsxattr_size() -> None:
    # struct fsxattr is five u32 values followed by eight pad bytes.  The ioctl
    # request embeds that exact 28-byte ABI size in bits 16..29.
    assert FSXATTR_STRUCT_SIZE == 28
    assert (FS_IOC_FSGETXATTR >> 16) & 0x3FFF == FSXATTR_STRUCT_SIZE


def test_linux_acl_external_evidence_is_not_silently_empty(tmp_path: Path, urn_factory) -> None:
    payload = tmp_path / "acl.dat"
    payload.write_bytes(b"acl")
    result = _observer().observe(
        ObservationRequest(path=payload, lineage_id=urn_factory(), host_id=urn_factory())
    )
    acl_rows = [
        row
        for row in result.state["filesystem_metadata"]["native_metadata"]
        if row["kind"] == "acl" and row["source"]["api"] == "acl_get_fd(3)"
    ]
    if acl_rows:
        assert acl_rows[0]["observed_byte_length"] > 0
        assert acl_rows[0]["value"]["byte_length"] > 0


def test_primary_read_length_mismatch_is_never_accepted_as_stable(
    tmp_path: Path, urn_factory, monkeypatch
) -> None:
    import riverhog_provenance.common as common

    payload = tmp_path / "short-read.dat"
    payload.write_bytes(b"abcdef")
    real_hash_fd = common.hash_fd

    def short_hash(fd: int, *, chunk_bytes: int):
        digest, _ = real_hash_fd(fd, chunk_bytes=chunk_bytes)
        return digest, 5

    monkeypatch.setattr(common, "hash_fd", short_hash)
    with pytest.raises(UnstableFileError, match="primary_read_size"):
        _observer().observe(
            ObservationRequest(
                path=payload,
                lineage_id=urn_factory(),
                host_id=urn_factory(),
            )
        )


def test_non_strict_mode_still_rejects_incomplete_content_fixity(
    tmp_path: Path, urn_factory, monkeypatch
) -> None:
    import riverhog_provenance.common as common

    payload = tmp_path / "partial-short-read.dat"
    payload.write_bytes(b"abcdef")
    real_hash_fd = common.hash_fd

    def short_hash(fd: int, *, chunk_bytes: int):
        digest, _ = real_hash_fd(fd, chunk_bytes=chunk_bytes)
        return digest, 5

    monkeypatch.setattr(common, "hash_fd", short_hash)
    with pytest.raises(UnstableFileError, match="primary_read_size"):
        _observer().observe(
            ObservationRequest(
                path=payload,
                lineage_id=urn_factory(),
                host_id=urn_factory(),
                policy=ObservationPolicy(strict_consistency=False),
                payload_binding=PayloadBindingRequest(),
            )
        )


class _ACLXattrOnlyNative:
    libacl = None

    @staticmethod
    def list_xattrs(fd: int):
        return [b"system.posix_acl_access"]

    @staticmethod
    def get_xattr(fd: int, name: bytes, maximum: int):
        value = b"opaque-acl-xattr"
        return len(value), value


def test_acl_xattr_counts_as_access_control_evidence_without_libacl(urn_factory) -> None:
    request = ObservationRequest(path="unused", lineage_id=urn_factory(), host_id=urn_factory())
    backend = LinuxBackend(
        native=_ACLXattrOnlyNative(),
        enforce_platform=False,
    )
    collection = NativeCollection()
    backend._capture_xattrs(0, request, collection)
    backend._capture_acl(0, request, collection)
    assert collection.coverage["access_control"] == "complete"
    assert collection.native_metadata[0]["kind"] == "acl"


def test_mountinfo_surrogate_bytes_get_portable_lossless_display() -> None:
    assert _portable_mount_field("source-\udcff") == "bytes:source-%FF"


class _LargeXattrNative:
    libacl = None

    @staticmethod
    def list_xattrs(fd: int):
        return [b"user.large"]

    @staticmethod
    def get_xattr(fd: int, name: bytes, maximum: int):
        return maximum + 1, None


def test_policy_not_retained_xattr_does_not_make_enumeration_partial(urn_factory) -> None:
    request = ObservationRequest(
        path="unused",
        lineage_id=urn_factory(),
        host_id=urn_factory(),
        policy=ObservationPolicy(inline_native_value_bytes=4, maximum_native_value_bytes=8),
    )
    backend = LinuxBackend(
        native=_LargeXattrNative(),
        enforce_platform=False,
    )
    collection = NativeCollection()
    backend._capture_xattrs(0, request, collection)
    assert collection.coverage["extended_attributes"] == "complete"
    assert collection.native_metadata[0]["capture_status"] == "not_retained"


def test_unexpected_getflags_failure_is_partial_not_complete(urn_factory, monkeypatch) -> None:
    import riverhog_provenance.linux as linux_module

    backend = LinuxBackend(
        native=_ACLXattrOnlyNative(),
        enforce_platform=False,
    )
    stat_snapshot = NativeStat(
        device=1,
        inode=2,
        mode=0o100644,
        nlink=1,
        uid=1000,
        gid=1000,
        size=3,
        atime_ns=1,
        mtime_ns=2,
        ctime_ns=3,
        extras={
            "statx_available": True,
            "statx_attributes": 0,
            "statx_attributes_mask": 0,
        },
    )

    def denied(*args, **kwargs):
        raise OSError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(linux_module.fcntl, "ioctl", denied)
    collection = NativeCollection()
    backend._capture_file_flags(
        0,
        stat_snapshot,
        request=ObservationRequest(path="unused", lineage_id=urn_factory(), host_id=urn_factory()),
        result=collection,
    )
    assert collection.coverage["file_flags"] == "partial"
    assert collection.diagnostics[0]["severity"] == "error"
