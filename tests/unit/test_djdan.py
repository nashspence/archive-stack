from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import djdan.main as djdan_main
from riverhog_core.domain.errors import HashMismatch
from tests.fixtures.data import fixture_encrypt_bytes

runner = CliRunner()


def _manifest_for(plaintext: bytes) -> dict[str, object]:
    sha256 = hashlib.sha256(plaintext).hexdigest()
    recovery = fixture_encrypt_bytes(plaintext)
    return {
        "id": "fx-1",
        "target": "docs/tax/2022/invoice-123.pdf",
        "entries": [
            {
                "id": "e1",
                "collection_id": "docs",
                "path": "tax/2022/invoice-123.pdf",
                "bytes": len(plaintext),
                "sha256": sha256,
                "recovery_bytes": len(recovery),
                "parts": [
                    {
                        "index": 0,
                        "bytes": len(plaintext),
                        "sha256": sha256,
                        "recovery_bytes": len(recovery),
                        "copies": [
                            {
                                "copy": "20260420T040001Z-1",
                                "location": "vault-a/shelf-01",
                                "disc_path": "disc/000001.bin",
                                "recovery_bytes": len(recovery),
                                "recovery_sha256": hashlib.sha256(recovery).hexdigest(),
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_default_djdan_io_builders_use_real_backends(monkeypatch) -> None:
    monkeypatch.delenv("DJDAN_READER_FACTORY", raising=False)
    monkeypatch.delenv("DJDAN_BURNER_FACTORY", raising=False)
    monkeypatch.delenv("DJDAN_BURNED_MEDIA_VERIFIER_FACTORY", raising=False)

    assert isinstance(djdan_main.build_optical_reader(), djdan_main.XorrisoOpticalReader)
    if djdan_main.sys.platform == "darwin":
        assert isinstance(djdan_main.build_disc_burner(), djdan_main.HdiutilDiscBurner)
    else:
        assert isinstance(djdan_main.build_disc_burner(), djdan_main.XorrisoDiscBurner)
    assert isinstance(
        djdan_main.build_burned_media_verifier(),
        djdan_main.RawBurnedMediaVerifier,
    )


def test_djdan_disc_list_image_scope_returns_paged_payload(monkeypatch) -> None:
    class FakeClient:
        def list_discs(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            query: str | None,
            image_id: str | None,
        ) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            assert page == 1
            assert per_page == 1
            assert sort == "id"
            assert order == "asc"
            assert query is None
            return {
                "page": page,
                "per_page": per_page,
                "total": 2,
                "pages": 2,
                "sort": sort,
                "order": order,
                "query": query,
                "image_id": image_id,
                "discs": [
                    {
                        "id": "20260420T040001Z-1",
                        "image_id": "20260420T040001Z",
                        "state": "verified",
                        "verification_state": "verified",
                        "location": "Shelf B1",
                    },
                ],
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)

    result = runner.invoke(
        djdan_main.app,
        [
            "disc",
            "list",
            "20260420T040001Z",
            "--page",
            "1",
            "--per-page",
            "1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["image_id"] == "20260420T040001Z"
    assert payload["page"] == 1
    assert payload["per_page"] == 1
    assert payload["total"] == 2
    assert payload["pages"] == 2
    assert payload["discs"] == [
        {
            "id": "20260420T040001Z-1",
            "image_id": "20260420T040001Z",
            "state": "verified",
            "verification_state": "verified",
            "location": "Shelf B1",
        }
    ]
    assert "copies" not in payload


def test_djdan_disc_show_uses_disc_endpoint(monkeypatch) -> None:
    class FakeClient:
        def get_disc(self, copy_id: str) -> dict[str, object]:
            assert copy_id == "20260420T040001Z-1"
            return {
                "id": copy_id,
                "image_id": "20260420T040001Z",
                "volume_id": "20260420T040001Z",
                "label_text": copy_id,
                "location": "Shelf B1",
                "created_at": "2026-04-20T04:00:01Z",
                "state": "registered",
                "verification_state": "verified",
                "history": [],
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)

    result = runner.invoke(
        djdan_main.app,
        ["disc", "show", "20260420T040001Z-1", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["id"] == "20260420T040001Z-1"
    assert payload["image_id"] == "20260420T040001Z"


def test_terminal_burn_prompts_reprompt_for_label_confirmation(monkeypatch, capsys) -> None:
    responses = iter(["", "labeled"])
    monkeypatch.setattr("builtins.input", lambda: next(responses))

    djdan_main.TerminalBurnPrompts().confirm_label("20260531T030858Z-2", label_text="copy-label")

    stderr = capsys.readouterr().err
    assert 'Type "labeled" after writing "copy-label" on disc 20260531T030858Z-2.' in stderr
    assert 'label confirmation for 20260531T030858Z-2 is still pending; type "labeled"' in stderr


def test_terminal_burn_prompt_names_required_media_capacity(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda: "")

    djdan_main.TerminalBurnPrompts().wait_for_blank_disc(
        "20260527T165916Z-1",
        device="default",
        target_bytes=50_000_000_000,
    )

    assert (
        "Insert blank media with at least 50.0 GB capacity for 20260527T165916Z-1"
        in capsys.readouterr().err
    )


def test_terminal_burn_prompts_accept_quoted_label_confirmation(monkeypatch) -> None:
    responses = iter(['"labeled"'])
    monkeypatch.setattr("builtins.input", lambda: next(responses))

    djdan_main.TerminalBurnPrompts().confirm_label("20260531T030858Z-2", label_text="copy-label")


def test_xorriso_optical_reader_reads_from_mounted_media(tmp_path: Path) -> None:
    payload_path = tmp_path / "disc" / "000001.bin"
    payload_path.parent.mkdir()
    payload_path.write_bytes(b"recovered-bytes")

    chunks = list(
        djdan_main.XorrisoOpticalReader().read_iter(
            "disc/000001.bin",
            device=str(tmp_path),
        )
    )

    assert chunks == [b"recovered-bytes"]


def test_run_checked_reports_heartbeat_for_silent_long_stage(monkeypatch, capsys) -> None:
    def fake_run(command, *, capture_output, text, check):
        assert command == ["fixture-tool"]
        assert capture_output is True
        assert text is True
        assert check is False
        time.sleep(0.04)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)
    monkeypatch.setattr(djdan_main, "_LOCAL_STAGE_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    djdan_main._run_checked(["fixture-tool"], action="fixture local stage")

    stderr = capsys.readouterr().err
    assert "fixture local stage still running after" in stderr
    assert "fixture local stage completed in" in stderr


def test_xorriso_optical_reader_extracts_from_device_with_xorriso(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, *, capture_output, text, check):
        assert capture_output is True
        assert text is True
        assert check is False
        commands.append(command)
        Path(command[-1]).write_bytes(b"device-bytes")
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    chunks = list(
        djdan_main.XorrisoOpticalReader().read_iter(
            "disc/000001.bin",
            device=str(tmp_path / "sr0"),
        )
    )

    assert chunks == [b"device-bytes"]
    assert commands == [
        [
            "/usr/bin/xorriso",
            "-osirrox",
            "on",
            "-indev",
            str(tmp_path / "sr0"),
            "-extract",
            "/disc/000001.bin",
            commands[0][-1],
        ]
    ]


def test_xorriso_disc_burner_invokes_xorriso_cdrecord(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []

    def fake_run(command, *, capture_output, text, check):
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    djdan_main.XorrisoDiscBurner().burn(
        iso_path,
        device="/dev/sr0",
        copy_id="20260420T040001Z-1",
    )

    assert commands == [
        [
            "/usr/bin/xorriso",
            "-as",
            "cdrecord",
            "-v",
            "dev=/dev/sr0",
            str(iso_path),
        ]
    ]


def test_xorriso_disc_burner_can_run_dummy_burn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []

    def fake_run(command, *, capture_output, text, check):
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    djdan_main.XorrisoDiscBurner(dummy=True).burn(
        iso_path,
        device="/dev/sr0",
        copy_id="20260420T040001Z-1",
    )

    assert commands == [
        [
            "/usr/bin/xorriso",
            "-as",
            "cdrecord",
            "-v",
            "-dummy",
            "dev=/dev/sr0",
            str(iso_path),
        ]
    ]


def test_hdiutil_disc_burner_validates_macos_device_path_and_runs_test_burn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []

    diskutil_output = """
   Device Identifier:         disk4
   Device Node:               /dev/disk4
   Device / Media Name:       PIONEER BD-RW BDR-UD03
   Optical Drive Type:        CD-ROM, CD-R, CD-RW, DVD-ROM, DVD-R, BD-ROM, BD-R, BD-RE
"""

    def fake_run(command, **kwargs):
        assert kwargs.get("check") is False
        if command == ["/usr/bin/diskutil", "info", "/dev/disk4"]:
            assert kwargs == {"capture_output": True, "text": True, "check": False}
            return djdan_main.subprocess.CompletedProcess(command, 0, diskutil_output, "")
        assert kwargs == {"check": False}
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    djdan_main.HdiutilDiscBurner(dummy=True).burn(
        iso_path,
        device="/dev/disk4",
        copy_id="20260420T040001Z-1",
    )

    assert commands == [
        [
            "/usr/bin/hdiutil",
            "burn",
            "-speed",
            "max",
            "-noverifyburn",
            "-noeject",
            "-testburn",
            str(iso_path),
        ]
    ]


def test_hdiutil_disc_burner_allows_native_hdiutil_target(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        assert kwargs == {"check": False}
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    djdan_main.HdiutilDiscBurner(dummy=True).burn(
        iso_path,
        device="hdiutil:IOService:/AppleARMPE/example/IOBDServices",
        copy_id="20260420T040001Z-1",
    )

    assert commands == [
        [
            "/usr/bin/hdiutil",
            "burn",
            "-device",
            "IOService:/AppleARMPE/example/IOBDServices",
            "-speed",
            "max",
            "-noverifyburn",
            "-noeject",
            "-testburn",
            str(iso_path),
        ]
    ]


def test_hdiutil_disc_burner_uses_native_verify_and_eject_for_real_burn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []
    diskutil_output = """
   Device Identifier:         disk4
   Device Node:               /dev/disk4
   Device / Media Name:       PIONEER BD-RW BDR-UD03
   Optical Drive Type:        CD-ROM, CD-R, CD-RW, DVD-ROM, DVD-R, BD-ROM, BD-R, BD-RE
"""

    def fake_run(command, **kwargs):
        assert kwargs.get("check") is False
        if command == ["/usr/bin/diskutil", "info", "/dev/disk4"]:
            assert kwargs == {"capture_output": True, "text": True, "check": False}
            return djdan_main.subprocess.CompletedProcess(command, 0, diskutil_output, "")
        assert kwargs == {"check": False}
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    burner = djdan_main.HdiutilDiscBurner()
    burner.burn(
        iso_path,
        device="/dev/disk4",
        copy_id="20260420T040001Z-1",
    )

    assert burner.verifies_media is True
    assert commands == [
        [
            "/usr/bin/hdiutil",
            "burn",
            "-speed",
            "max",
            "-verifyburn",
            "-eject",
            str(iso_path),
        ]
    ]


def test_hdiutil_disc_burner_reports_failed_real_verify_as_suspect_media(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    diskutil_output = """
   Device Identifier:         disk4
   Device Node:               /dev/disk4
   Device / Media Name:       PIONEER BD-RW BDR-UD03
   Optical Drive Type:        CD-ROM, CD-R, CD-RW, DVD-ROM, DVD-R, BD-ROM, BD-R, BD-RE
"""

    def fake_run(command, **kwargs):
        assert kwargs.get("check") is False
        if command == ["/usr/bin/diskutil", "info", "/dev/disk4"]:
            assert kwargs == {"capture_output": True, "text": True, "check": False}
            return djdan_main.subprocess.CompletedProcess(command, 0, diskutil_output, "")
        assert kwargs == {"check": False}
        return djdan_main.subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    with pytest.raises(
        djdan_main.BurnedMediaVerificationError,
        match="hdiutil did not complete a verified burn",
    ):
        djdan_main.HdiutilDiscBurner().burn(
            iso_path,
            device="/dev/disk4",
            copy_id="20260420T040001Z-1",
        )


def test_hdiutil_disc_burner_rejects_native_bd_testburn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    iso_path.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []
    diskutil_output = """
   Device Identifier:         disk4
   Device Node:               /dev/disk4
   Device / Media Name:       PIONEER BD-RW BDR-UD03
   Optical Drive Type:        CD-ROM, CD-R, CD-RW, DVD-ROM, DVD-R, BD-ROM, BD-R, BD-RE
   Optical Media Type:        BD-R
"""

    def fake_run(command, **kwargs):
        if command == ["/usr/bin/diskutil", "info", "/dev/disk4"]:
            assert kwargs == {"capture_output": True, "text": True, "check": False}
            return djdan_main.subprocess.CompletedProcess(command, 0, diskutil_output, "")
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="native test burns are not available for BD-R"):
        djdan_main.HdiutilDiscBurner(dummy=True).burn(
            iso_path,
            device="/dev/disk4",
            copy_id="20260420T040001Z-1",
        )

    assert commands == []


def test_raw_burned_media_verifier_compares_the_iso_prefix(tmp_path: Path) -> None:
    iso_path = tmp_path / "image.iso"
    device_path = tmp_path / "sr0"
    iso_path.write_bytes(b"iso-bytes")
    device_path.write_bytes(b"iso-bytes" + b"\0" * 2048)

    djdan_main.RawBurnedMediaVerifier().verify(
        iso_path,
        device=str(device_path),
        copy_id="20260420T040001Z-1",
    )

    device_path.write_bytes(b"bad-bytes" + b"\0" * 2048)
    with pytest.raises(RuntimeError, match="burned media verification failed"):
        djdan_main.RawBurnedMediaVerifier().verify(
            iso_path,
            device=str(device_path),
            copy_id="20260420T040001Z-1",
        )


def test_raw_burned_media_verifier_unmounts_macos_optical_media_before_raw_read(
    monkeypatch,
    tmp_path: Path,
) -> None:
    iso_path = tmp_path / "image.iso"
    raw_device = tmp_path / "rdisk4"
    iso_path.write_bytes(b"iso-bytes")
    raw_device.write_bytes(b"iso-bytes")
    commands: list[list[str]] = []
    diskutil_output = """
   Device Identifier:         disk4
   Device Node:               /dev/disk4
   Volume Name:               20260526T204059Z
   Mounted:                   Yes
   Mount Point:               /Volumes/20260526T204059Z
   Device / Media Name:       PIONEER BD-RW BDR-UD03
   Optical Drive Type:        CD-ROM, CD-R, CD-RW, DVD-ROM, DVD-R, BD-ROM, BD-R, BD-RE
"""

    def fake_run(command, *, capture_output, text, check):
        assert check is False
        commands.append(command)
        if command == ["/usr/bin/diskutil", "info", "/dev/disk4"]:
            assert capture_output is True
            assert text is True
            return djdan_main.subprocess.CompletedProcess(command, 0, diskutil_output, "")
        if command == ["/usr/bin/diskutil", "unmountDisk", "/dev/disk4"]:
            assert capture_output is True
            assert text is True
            return djdan_main.subprocess.CompletedProcess(command, 0, "Unmount successful", "")
        raise AssertionError(f"unexpected command: {command}")

    original_open = Path.open

    monkeypatch.setattr(djdan_main.sys, "platform", "darwin")
    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: "/usr/bin/diskutil")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)
    monkeypatch.setattr(
        djdan_main.Path,
        "open",
        lambda path, mode="r", *args, **kwargs: (
            original_open(raw_device, mode, *args, **kwargs)
            if str(path) == "/dev/rdisk4"
            else original_open(path, mode, *args, **kwargs)
        ),
    )

    djdan_main.RawBurnedMediaVerifier().verify(
        iso_path,
        device="/dev/disk4",
        copy_id="20260420T040001Z-1",
    )

    assert commands == [
        ["/usr/bin/diskutil", "info", "/dev/disk4"],
        ["/usr/bin/diskutil", "unmountDisk", "/dev/disk4"],
    ]


def test_djdan_fetch_recovers_in_memory_and_reports_progress(monkeypatch) -> None:
    plaintext = b"invoice fixture bytes\n"
    recovered = fixture_encrypt_bytes(plaintext)
    uploaded: list[tuple[str, int, str, bytes]] = []

    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return _manifest_for(plaintext)

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            assert fetch_id == "fx-1"
            assert entry_id == "e1"
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": "https://uploads.test/fx-1/e1",
                "offset": 0,
                "length": len(recovered),
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            uploaded.append((upload_url, offset, checksum_algorithm, content))
            return {"offset": offset + len(content), "expires_at": None}

        def complete_fetch(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return {"id": fetch_id, "state": "done"}

    class FakeReader:
        def read_iter(self, disc_path: str, *, device: str):
            assert disc_path == "disc/000001.bin"
            assert device == "/dev/fake-sr0"
            yield recovered[:8]
            yield recovered[8:]

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n",
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"fetches": [{"id": "fx-1", "state": "done"}]}
    assert "20260420T040001Z-1" in result.stderr
    assert "current file" in result.stderr
    assert "manifest" in result.stderr
    assert "/s" in result.stderr
    assert uploaded == [
        ("https://uploads.test/fx-1/e1", 0, "sha256", recovered[:8]),
        ("https://uploads.test/fx-1/e1", 8, "sha256", recovered[8:]),
    ]


def test_djdan_fetch_prompts_when_split_file_needs_next_disc(
    monkeypatch,
) -> None:
    part_one_plaintext = b"invoice fixture "
    part_two_plaintext = b"bytes\n"
    part_one = fixture_encrypt_bytes(part_one_plaintext)
    part_two = fixture_encrypt_bytes(part_two_plaintext)
    uploaded: list[tuple[int, bytes]] = []

    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return {
                "id": "fx-1",
                "target": "docs/tax/2022/invoice-123.pdf",
                "entries": [
                    {
                        "id": "e1",
                        "collection_id": "docs",
                        "path": "tax/2022/invoice-123.pdf",
                        "bytes": len(part_one_plaintext) + len(part_two_plaintext),
                        "sha256": hashlib.sha256(
                            part_one_plaintext + part_two_plaintext
                        ).hexdigest(),
                        "recovery_bytes": len(part_one) + len(part_two),
                        "parts": [
                            {
                                "index": 0,
                                "bytes": len(part_one_plaintext),
                                "sha256": hashlib.sha256(part_one_plaintext).hexdigest(),
                                "recovery_bytes": len(part_one),
                                "copies": [
                                    {
                                        "copy": "20260420T040003Z-1",
                                        "location": "vault-a/shelf-01",
                                        "disc_path": "disc/000001.bin",
                                        "recovery_bytes": len(part_one),
                                        "recovery_sha256": hashlib.sha256(part_one).hexdigest(),
                                    }
                                ],
                            },
                            {
                                "index": 1,
                                "bytes": len(part_two_plaintext),
                                "sha256": hashlib.sha256(part_two_plaintext).hexdigest(),
                                "recovery_bytes": len(part_two),
                                "copies": [
                                    {
                                        "copy": "20260420T040004Z-1",
                                        "location": "vault-a/shelf-02",
                                        "disc_path": "disc/000002.bin",
                                        "recovery_bytes": len(part_two),
                                        "recovery_sha256": hashlib.sha256(part_two).hexdigest(),
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": "https://uploads.test/fx-1/e1",
                "offset": 0,
                "length": len(part_one) + len(part_two),
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            _ = upload_url, checksum_algorithm
            uploaded.append((offset, content))
            return {"offset": offset + len(content), "expires_at": None}

        def complete_fetch(self, fetch_id: str) -> dict[str, object]:
            return {"id": fetch_id, "state": "done"}

    class FakeReader:
        def read_iter(self, disc_path: str, *, device: str):
            assert device == "/dev/fake-sr0"
            if disc_path == "disc/000001.bin":
                yield part_one
                return
            if disc_path == "disc/000002.bin":
                yield part_two
                return
            raise AssertionError(f"unexpected disc path: {disc_path}")

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n\n",
    )

    assert result.exit_code == 0
    assert "Insert disc 20260420T040003Z-1 from vault-a/shelf-01" in result.stderr
    assert "Insert disc 20260420T040004Z-1 from vault-a/shelf-02" in result.stderr
    assert uploaded == [
        (0, part_one),
        (len(part_one), part_two),
    ]


def test_djdan_fetch_prompt_state_spans_manifest_entries(
    monkeypatch,
) -> None:
    first_plaintext = b"first collection file\n"
    second_plaintext = b"second collection file\n"
    first_recovery = fixture_encrypt_bytes(first_plaintext)
    second_recovery = fixture_encrypt_bytes(second_plaintext)
    uploaded: list[tuple[str, int, bytes]] = []

    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return {
                "id": "fx-1",
                "target": "2025/",
                "entries": [
                    {
                        "id": "e1",
                        "collection_id": "2025/alpha",
                        "path": "alpha/file.txt",
                        "bytes": len(first_plaintext),
                        "sha256": hashlib.sha256(first_plaintext).hexdigest(),
                        "recovery_bytes": len(first_recovery),
                        "parts": [
                            {
                                "index": 0,
                                "bytes": len(first_plaintext),
                                "sha256": hashlib.sha256(first_plaintext).hexdigest(),
                                "recovery_bytes": len(first_recovery),
                                "copies": [
                                    {
                                        "copy": "20260420T040003Z-1",
                                        "location": "vault-a/shelf-01",
                                        "disc_path": "disc/000001.bin",
                                        "recovery_bytes": len(first_recovery),
                                        "recovery_sha256": hashlib.sha256(
                                            first_recovery
                                        ).hexdigest(),
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "id": "e2",
                        "collection_id": "2025/beta",
                        "path": "beta/file.txt",
                        "bytes": len(second_plaintext),
                        "sha256": hashlib.sha256(second_plaintext).hexdigest(),
                        "recovery_bytes": len(second_recovery),
                        "parts": [
                            {
                                "index": 0,
                                "bytes": len(second_plaintext),
                                "sha256": hashlib.sha256(second_plaintext).hexdigest(),
                                "recovery_bytes": len(second_recovery),
                                "copies": [
                                    {
                                        "copy": "20260420T040004Z-1",
                                        "location": "vault-a/shelf-02",
                                        "disc_path": "disc/000002.bin",
                                        "recovery_bytes": len(second_recovery),
                                        "recovery_sha256": hashlib.sha256(
                                            second_recovery
                                        ).hexdigest(),
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            lengths = {"e1": len(first_recovery), "e2": len(second_recovery)}
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": f"https://uploads.test/{fetch_id}/{entry_id}",
                "offset": 0,
                "length": lengths[entry_id],
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            _ = checksum_algorithm
            uploaded.append((upload_url, offset, content))
            return {"offset": offset + len(content), "expires_at": None}

        def complete_fetch(self, fetch_id: str) -> dict[str, object]:
            return {"id": fetch_id, "state": "done"}

    class FakeReader:
        def read_iter(self, disc_path: str, *, device: str):
            assert device == "/dev/fake-sr0"
            if disc_path == "disc/000001.bin":
                yield first_recovery
                return
            if disc_path == "disc/000002.bin":
                yield second_recovery
                return
            raise AssertionError(f"unexpected disc path: {disc_path}")

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n\n",
    )

    assert result.exit_code == 0
    assert "Insert disc 20260420T040003Z-1 from vault-a/shelf-01" in result.stderr
    assert "Insert disc 20260420T040004Z-1 from vault-a/shelf-02" in result.stderr
    assert uploaded == [
        ("https://uploads.test/fx-1/e1", 0, first_recovery),
        ("https://uploads.test/fx-1/e2", 0, second_recovery),
    ]


def test_djdan_fetch_does_not_reprompt_for_same_disc_across_entries(
    monkeypatch,
) -> None:
    first_plaintext = b"first file\n"
    second_plaintext = b"second file\n"
    first_recovery = fixture_encrypt_bytes(first_plaintext)
    second_recovery = fixture_encrypt_bytes(second_plaintext)
    uploaded: list[tuple[str, int, bytes]] = []

    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return {
                "id": "fx-1",
                "target": "2025/",
                "entries": [
                    {
                        "id": "e1",
                        "collection_id": "2025/alpha",
                        "path": "alpha/file.txt",
                        "bytes": len(first_plaintext),
                        "sha256": hashlib.sha256(first_plaintext).hexdigest(),
                        "recovery_bytes": len(first_recovery),
                        "parts": [
                            {
                                "index": 0,
                                "bytes": len(first_plaintext),
                                "sha256": hashlib.sha256(first_plaintext).hexdigest(),
                                "recovery_bytes": len(first_recovery),
                                "copies": [
                                    {
                                        "copy": "20260420T040003Z-1",
                                        "location": "vault-a/shelf-01",
                                        "disc_path": "disc/000001.bin",
                                        "recovery_bytes": len(first_recovery),
                                        "recovery_sha256": hashlib.sha256(
                                            first_recovery
                                        ).hexdigest(),
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "id": "e2",
                        "collection_id": "2025/beta",
                        "path": "beta/file.txt",
                        "bytes": len(second_plaintext),
                        "sha256": hashlib.sha256(second_plaintext).hexdigest(),
                        "recovery_bytes": len(second_recovery),
                        "parts": [
                            {
                                "index": 0,
                                "bytes": len(second_plaintext),
                                "sha256": hashlib.sha256(second_plaintext).hexdigest(),
                                "recovery_bytes": len(second_recovery),
                                "copies": [
                                    {
                                        "copy": "20260420T040003Z-1",
                                        "location": "vault-a/shelf-01",
                                        "disc_path": "disc/000002.bin",
                                        "recovery_bytes": len(second_recovery),
                                        "recovery_sha256": hashlib.sha256(
                                            second_recovery
                                        ).hexdigest(),
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            lengths = {"e1": len(first_recovery), "e2": len(second_recovery)}
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": f"https://uploads.test/{fetch_id}/{entry_id}",
                "offset": 0,
                "length": lengths[entry_id],
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            _ = checksum_algorithm
            uploaded.append((upload_url, offset, content))
            return {"offset": offset + len(content), "expires_at": None}

        def complete_fetch(self, fetch_id: str) -> dict[str, object]:
            return {"id": fetch_id, "state": "done"}

    class FakeReader:
        def read_iter(self, disc_path: str, *, device: str):
            assert device == "/dev/fake-sr0"
            if disc_path == "disc/000001.bin":
                yield first_recovery
                return
            if disc_path == "disc/000002.bin":
                yield second_recovery
                return
            raise AssertionError(f"unexpected disc path: {disc_path}")

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n",
    )

    assert result.exit_code == 0
    assert result.stderr.count("Insert disc 20260420T040003Z-1") == 1
    assert uploaded == [
        ("https://uploads.test/fx-1/e1", 0, first_recovery),
        ("https://uploads.test/fx-1/e2", 0, second_recovery),
    ]


def test_djdan_fetch_resets_byte_complete_upload_after_final_verification_failure(
    monkeypatch,
) -> None:
    plaintext = b"invoice fixture bytes\n"
    recovered = fixture_encrypt_bytes(plaintext)
    cancelled: list[tuple[str, str]] = []

    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return _manifest_for(plaintext)

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": "https://uploads.test/fx-1/e1",
                "offset": 0,
                "length": len(recovered),
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            return {"offset": offset + len(content), "expires_at": None}

        def complete_fetch(self, fetch_id: str) -> dict[str, object]:
            raise HashMismatch("sha256 did not match")

        def cancel_fetch_entry_upload(self, fetch_id: str, entry_id: str) -> None:
            cancelled.append((fetch_id, entry_id))

    class FakeReader:
        def read_iter(self, disc_path: str, *, device: str):
            yield recovered

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0"],
        input="\n",
    )

    assert result.exit_code == 1
    assert cancelled == [("fx-1", "e1")]
    assert "reset byte-complete upload for docs/tax/2022/invoice-123.pdf" in result.stderr
    assert "try another registered copy or recovered media" in result.stderr
    assert "error: final fetch verification failed: sha256 did not match" in result.stderr


def test_djdan_fetch_reports_clean_error_when_optical_read_fails(monkeypatch) -> None:
    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            return _manifest_for(b"invoice fixture bytes\n")

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            recovery = fixture_encrypt_bytes(b"invoice fixture bytes\n")
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": "https://uploads.test/fx-1/e1",
                "offset": 0,
                "length": len(recovery),
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

    class FailingReader:
        def read_iter(self, disc_path: str, *, device: str):
            raise RuntimeError(f"fixture optical read failed for {disc_path} on {device}")

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FailingReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0"],
        input="\n",
    )

    assert result.exit_code == 1
    assert (
        "error: fixture optical read failed for disc/000001.bin on /dev/fake-sr0" in result.stderr
    )
    assert "Traceback" not in result.stderr


def test_djdan_fetch_resumes_split_entry_from_session_offset(monkeypatch) -> None:
    part_one_plaintext = b"invoice fixture "
    part_two_plaintext = b"bytes\n"
    part_one = fixture_encrypt_bytes(part_one_plaintext)
    part_two = fixture_encrypt_bytes(part_two_plaintext)
    uploaded: list[tuple[int, bytes]] = []

    class FakeClient:
        def get_fetch_manifest(self, fetch_id: str) -> dict[str, object]:
            assert fetch_id == "fx-1"
            return {
                "id": "fx-1",
                "target": "docs/tax/2022/invoice-123.pdf",
                "entries": [
                    {
                        "id": "e1",
                        "collection_id": "docs",
                        "path": "tax/2022/invoice-123.pdf",
                        "bytes": len(part_one_plaintext) + len(part_two_plaintext),
                        "sha256": hashlib.sha256(
                            part_one_plaintext + part_two_plaintext
                        ).hexdigest(),
                        "recovery_bytes": len(part_one) + len(part_two),
                        "parts": [
                            {
                                "index": 0,
                                "bytes": len(part_one_plaintext),
                                "sha256": hashlib.sha256(part_one_plaintext).hexdigest(),
                                "recovery_bytes": len(part_one),
                                "copies": [
                                    {
                                        "copy": "20260420T040003Z-1",
                                        "location": "vault-a/shelf-01",
                                        "disc_path": "disc/000001.bin",
                                        "recovery_bytes": len(part_one),
                                        "recovery_sha256": hashlib.sha256(part_one).hexdigest(),
                                    }
                                ],
                            },
                            {
                                "index": 1,
                                "bytes": len(part_two_plaintext),
                                "sha256": hashlib.sha256(part_two_plaintext).hexdigest(),
                                "recovery_bytes": len(part_two),
                                "copies": [
                                    {
                                        "copy": "20260420T040004Z-1",
                                        "location": "vault-a/shelf-02",
                                        "disc_path": "disc/000002.bin",
                                        "recovery_bytes": len(part_two),
                                        "recovery_sha256": hashlib.sha256(part_two).hexdigest(),
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }

        def create_or_resume_fetch_entry_upload(
            self, fetch_id: str, entry_id: str
        ) -> dict[str, object]:
            return {
                "entry": entry_id,
                "protocol": "tus",
                "upload_url": "https://uploads.test/fx-1/e1",
                "offset": len(part_one),
                "length": len(part_one) + len(part_two),
                "checksum_algorithm": "sha256",
                "expires_at": "2026-04-23T00:00:00Z",
            }

        def append_upload_chunk(
            self,
            upload_url: str,
            *,
            offset: int,
            checksum_algorithm: str,
            content: bytes,
        ) -> dict[str, object]:
            assert upload_url == "https://uploads.test/fx-1/e1"
            assert checksum_algorithm == "sha256"
            uploaded.append((offset, content))
            return {"offset": offset + len(content), "expires_at": None}

        def complete_fetch(self, fetch_id: str) -> dict[str, object]:
            return {"id": fetch_id, "state": "done"}

    class FakeReader:
        def read_iter(self, disc_path: str, *, device: str):
            assert disc_path == "disc/000002.bin"
            assert device == "/dev/fake-sr0"
            yield part_two[:2]
            yield part_two[2:]

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        djdan_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n",
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"fetches": [{"id": "fx-1", "state": "done"}]}
    assert "20260420T040003Z-1" not in result.stderr
    assert "20260420T040004Z-1" in result.stderr
    assert uploaded == [
        (len(part_one), part_two[:2]),
        (len(part_one) + 2, part_two[2:]),
    ]


def test_discover_burn_backlog_prefers_fullest_ready_candidate() -> None:
    class FakeClient:
        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            assert (page, per_page, sort, order, iso_ready) == (1, 100, "fill", "desc", True)
            return {
                "page": 1,
                "pages": 1,
                "target_bytes": 50_000_000_000,
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
                        "bytes": 45_000_000_000,
                        "target_bytes": 50_000_000_000,
                        "fill": 0.9,
                        "iso_ready": True,
                    }
                ],
            }

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            assert (page, per_page, sort, order) == (1, 100, "finalized_at", "desc")
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040003Z",
                        "filename": "20260420T040003Z.iso",
                        "bytes": 25_000_000_000,
                        "target_bytes": 50_000_000_000,
                        "fill": 0.5,
                        "physical_copies_registered": 1,
                        "physical_copies_required": 2,
                    }
                ],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040003Z"
            return {
                "copies": [
                    {"id": "20260420T040003Z-1", "state": "verified"},
                    {"id": "20260420T040003Z-3", "state": "needed"},
                ]
            }

    backlog = djdan_main._discover_burn_backlog(FakeClient())

    assert [(item.candidate_id, item.image_id) for item in backlog] == [
        ("img_2026-04-20_01", None),
        (None, "20260420T040003Z"),
    ]
    assert [(item.expected_bytes, item.target_bytes) for item in backlog] == [
        (45_000_000_000, 50_000_000_000),
        (25_000_000_000, 50_000_000_000),
    ]


def test_discover_burn_backlog_skips_images_that_now_require_recovery_flow() -> None:
    class FakeClient:
        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 2,
                    }
                ],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "copies": [
                    {"id": "20260420T040001Z-1", "state": "lost"},
                    {"id": "20260420T040001Z-2", "state": "damaged"},
                    {"id": "20260420T040001Z-3", "state": "needed"},
                ]
            }

    assert djdan_main._discover_burn_backlog(FakeClient()) == []


def test_discover_recovery_handoffs_for_images_that_require_recovery() -> None:
    class FakeClient:
        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 2,
                    }
                ],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "copies": [
                    {"id": "20260420T040001Z-1", "state": "lost"},
                    {"id": "20260420T040001Z-2", "state": "damaged"},
                    {"id": "20260420T040001Z-3", "state": "needed"},
                ]
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "id": "rs-20260420T040001Z-1",
                "state": "restore_requested",
                "latest_message": (
                    "Archive restore requested; wait until the session is ready before "
                    "burning replacement media."
                ),
            }

    assert djdan_main._discover_recovery_handoffs(FakeClient()) == [
        djdan_main.RecoveryHandoff(
            image_id="20260420T040001Z",
            session_id="rs-20260420T040001Z-1",
            state="restore_requested",
            latest_message=(
                "Archive restore requested; wait until the session is ready before "
                "burning replacement media."
            ),
        )
    ]


def test_list_disc_rebuild_sessions_defaults_to_active_states() -> None:
    class FakeClient:
        def list_recovery_sessions(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            recovery_type: str,
            state: str | None = None,
        ) -> dict[str, object]:
            assert (per_page, sort, order, recovery_type) == (
                100,
                "created_at",
                "desc",
                "image_rebuild",
            )
            if state != "restore_requested":
                return {"page": page, "pages": 0, "sessions": []}
            return {
                "page": page,
                "pages": 1,
                "sessions": [
                    {
                        "id": "rs-20260420T040001Z-1",
                        "state": "restore_requested",
                        "latest_message": (
                            "Archive restore requested; wait until the session is ready "
                            "before burning replacement media."
                        ),
                        "images": [
                            {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                            {"id": "20260420T040003Z", "filename": "20260420T040003Z.iso"},
                        ],
                    }
                ],
            }

    payload = djdan_main._list_disc_rebuild_sessions(
        FakeClient(),
        page=1,
        per_page=25,
        sort="created_at",
        order="desc",
        state=None,
        include_all=False,
    )

    assert payload["total"] == 1
    assert payload["sessions"] == [
        {
            "id": "rs-20260420T040001Z-1",
            "state": "restore_requested",
            "latest_message": (
                "Archive restore requested; wait until the session is ready "
                "before burning replacement media."
            ),
            "images": [
                {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                {"id": "20260420T040003Z", "filename": "20260420T040003Z.iso"},
            ],
        }
    ]


def test_disc_rebuild_pause_and_resume_commands_emit_json(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def payload(state: str) -> dict[str, object]:
        return {
            "id": "rs-20260420T040001Z-rebuild-1",
            "type": "image_rebuild",
            "state": state,
            "created_at": "2026-04-20T04:00:00Z",
            "restore_requested_at": None,
            "restore_ready_at": None,
            "restore_expires_at": None,
            "completed_at": None,
            "canceled_at": None,
            "paused_at": "2026-04-20T04:00:00Z" if state == "paused" else None,
            "paused_from_state": "restore_requested" if state == "paused" else None,
            "restore_paths": None,
            "latest_message": f"session is {state}",
            "warnings": [],
            "notification": {
                "webhook_configured": True,
                "reminder_count": 0,
                "next_reminder_at": "2026-04-21T04:00:00Z" if state == "paused" else None,
                "last_notified_at": None,
                "failure_count": 0,
                "last_failure_at": None,
                "last_failure": None,
            },
            "progress": {
                "archive_verification": "pending",
                "extraction": "pending",
                "materialization": "pending",
            },
            "collections": [],
            "images": [
                {
                    "id": "20260420T040001Z",
                    "filename": "20260420T040001Z.iso",
                    "collection_ids": ["docs"],
                    "rebuild_state": state,
                }
            ],
        }

    class FakeClient:
        def pause_recovery_session(self, session_id: str) -> dict[str, object]:
            calls.append(("pause", session_id))
            return payload("paused")

        def resume_recovery_session(self, session_id: str) -> dict[str, object]:
            calls.append(("resume", session_id))
            return payload("restore_requested")

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)

    paused = runner.invoke(
        djdan_main.app,
        ["disc", "rebuild", "pause", "rs-20260420T040001Z-rebuild-1", "--json"],
    )
    resumed = runner.invoke(
        djdan_main.app,
        ["disc", "rebuild", "resume", "rs-20260420T040001Z-rebuild-1", "--json"],
    )

    assert paused.exit_code == 0
    assert resumed.exit_code == 0
    assert json.loads(paused.stdout)["state"] == "paused"
    assert json.loads(resumed.stdout)["state"] == "restore_requested"
    assert calls == [
        ("pause", "rs-20260420T040001Z-rebuild-1"),
        ("resume", "rs-20260420T040001Z-rebuild-1"),
    ]


def test_disc_rebuild_declares_lost_or_damaged_disc(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None]] = []

    class FakeClient:
        def update_copy(
            self,
            image_id: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ) -> dict[str, object]:
            assert location is None
            assert verification_state is None
            calls.append((image_id, copy_id, state))
            return {
                "copy": {
                    "id": copy_id,
                    "image_id": image_id,
                    "volume_id": image_id,
                    "label_text": copy_id,
                    "state": state,
                    "verification_state": "pending",
                }
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "id": "rs-20260420T040001Z-rebuild-1",
                "type": "image_rebuild",
                "state": "restore_requested",
                "latest_message": "Archive restore requested.",
                "images": [{"id": image_id, "filename": f"{image_id}.iso"}],
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)

    result = runner.invoke(
        djdan_main.app,
        ["disc", "rebuild", "start", "20260420T040001Z-1", "--reason", "lost", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["copy"]["state"] == "lost"
    assert payload["recovery_session"]["id"] == "rs-20260420T040001Z-rebuild-1"
    assert calls == [("20260420T040001Z", "20260420T040001Z-1", "lost")]


def test_all_pending_recovery_seed_slots_do_not_reenter_standard_burn_backlog() -> None:
    class FakeClient:
        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "id": "rs-20260420T040001Z-1",
                "state": "ready",
                "latest_message": "Restored ISO data is ready.",
                "images": [{"id": image_id, "filename": f"{image_id}.iso"}],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "copies": [
                    {"id": "20260420T040001Z-3", "state": "needed"},
                ]
            }

    assert not djdan_main._is_standard_burn_backlog_image(
        FakeClient(),
        "20260420T040001Z",
    )


def test_djdan_recover_lists_active_sessions(monkeypatch) -> None:
    class FakeClient:
        def list_recovery_sessions(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            recovery_type: str,
            state: str | None = None,
        ) -> dict[str, object]:
            assert recovery_type == "image_rebuild"
            if state not in {"restore_requested", "ready"}:
                return {"page": page, "pages": 0, "sessions": []}
            return {
                "page": page,
                "pages": 1,
                "sessions": [
                    {
                        "id": "rs-20260420T040001Z-1",
                        "state": "restore_requested",
                        "latest_message": (
                            "Archive restore requested; wait until the session is ready before "
                            "burning replacement media."
                        ),
                        "images": [
                            {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                        ],
                    }
                ]
                if state == "restore_requested"
                else [],
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)

    result = runner.invoke(djdan_main.app, ["disc", "rebuild", "list"])

    assert result.exit_code == 0
    assert "rs-20260420T040001Z-1" in result.stdout
    assert "restore_requested" in result.stdout
    assert "20260420T040001Z" in result.stdout


def test_djdan_recover_reports_waiting_session(monkeypatch, tmp_path: Path) -> None:
    class FakeClient:
        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 2,
                    }
                ],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "copies": [
                    {"id": "20260420T040001Z-1", "state": "lost"},
                    {"id": "20260420T040001Z-2", "state": "damaged"},
                    {"id": "20260420T040001Z-3", "state": "needed"},
                ]
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "id": "rs-20260420T040001Z-1",
                "state": "restore_requested",
                "latest_message": (
                    "Archive restore requested; wait until the session is ready before "
                    "burning replacement media."
                ),
                "images": [
                    {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                ],
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: object())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: object())
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: object())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: object())

    result = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
        input="\n",
    )

    assert result.exit_code == 0
    assert "burn backlog already clear" in result.stdout
    assert "burn backlog is waiting for image rebuild restore work" in result.stdout
    assert "rs-20260420T040001Z-1" in result.stdout
    assert "restore_requested" in result.stdout
    assert "Archive restore requested" in result.stdout


def test_djdan_recover_ready_session_burns_replacements_and_cleans_staging(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_id = "20260420T040001Z"

    class FakeClient:
        def __init__(self) -> None:
            self.iso_bytes = b"fixture-iso\n"
            self.completed_sessions: list[str] = []
            self.copy_states = {
                f"{image_id}-1": {
                    "id": f"{image_id}-1",
                    "label_text": f"{image_id}-1",
                    "state": "lost",
                    "verification_state": "pending",
                    "location": None,
                },
                f"{image_id}-2": {
                    "id": f"{image_id}-2",
                    "label_text": f"{image_id}-2",
                    "state": "damaged",
                    "verification_state": "pending",
                    "location": None,
                },
                f"{image_id}-3": {
                    "id": f"{image_id}-3",
                    "label_text": f"{image_id}-3",
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
            }

        def _verified_count(self) -> int:
            return sum(
                1
                for copy in self.copy_states.values()
                if copy["state"] in {"registered", "verified"}
            )

        def _ensure_followup_copy(self) -> None:
            if self._verified_count() == 1 and f"{image_id}-4" not in self.copy_states:
                self.copy_states[f"{image_id}-4"] = {
                    "id": f"{image_id}-4",
                    "label_text": f"{image_id}-4",
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                }

        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": image_id,
                        "filename": f"{image_id}.iso",
                        "fill": 0.9,
                        "physical_copies_registered": self._verified_count(),
                        "physical_copies_required": 2,
                    }
                ],
            }

        def get_recovery_session(self, session_id: str) -> dict[str, object]:
            return {
                "id": session_id,
                "state": "ready",
                "latest_message": "Restored ISO data is ready.",
                "images": [{"id": image_id, "filename": f"{image_id}.iso"}],
            }

        def get_recovery_session_for_image(self, image_id_arg: str) -> dict[str, object]:
            assert image_id_arg == image_id
            return self.get_recovery_session("rs-20260420T040001Z-1")

        def list_copies(self, image_id_arg: str) -> dict[str, object]:
            assert image_id_arg == image_id
            self._ensure_followup_copy()
            return {"copies": list(self.copy_states.values())}

        def download_recovered_iso(
            self,
            session_id: str,
            image_id_arg: str,
            output: Path,
        ) -> bytes:
            assert session_id == "rs-20260420T040001Z-1"
            assert image_id_arg == image_id
            output.write_bytes(self.iso_bytes)
            return self.iso_bytes

        def register_copy(self, image_id_arg: str, location: str, *, copy_id: str | None = None):
            assert image_id_arg == image_id
            assert copy_id is not None
            self.copy_states[copy_id]["state"] = "registered"
            self.copy_states[copy_id]["location"] = location
            return {"copy": self.copy_states[copy_id]}

        def update_copy(
            self,
            image_id_arg: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            assert image_id_arg == image_id
            copy = self.copy_states[copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

        def complete_recovery_session(self, session_id: str) -> dict[str, object]:
            self.completed_sessions.append(session_id)
            return {"id": session_id, "state": "completed"}

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakeBurner:
        def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert iso_path.exists()

    class FakeMediaVerifier:
        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakePrompts:
        def __init__(self) -> None:
            self.locations = {
                f"{image_id}-3": "vault-a/shelf-02",
                f"{image_id}-4": "vault-b/shelf-02",
            }

        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            assert device == "/dev/fake-sr0"

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            assert label_text == copy_id

        def prompt_location(self, copy_id: str) -> str:
            return self.locations[copy_id]

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return True

    client = FakeClient()
    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: FakeBurner())
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: FakePrompts())

    result = runner.invoke(
        djdan_main.app,
        [
            "burn",
            "--device",
            "/dev/fake-sr0",
            "--staging-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "burn backlog cleared" in result.stdout
    assert f"{image_id}-3" in result.stdout
    assert f"{image_id}-4" in result.stdout
    assert client.completed_sessions == ["rs-20260420T040001Z-1"]
    assert client.copy_states[f"{image_id}-3"]["state"] == "verified"
    assert client.copy_states[f"{image_id}-4"]["state"] == "verified"
    assert not (tmp_path / image_id).exists()
    assert not (tmp_path / "burn-session.json").exists()


def test_djdan_recover_can_finish_expired_session_from_local_staging(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_id = "20260420T040001Z"
    iso_path = tmp_path / image_id / f"{image_id}.iso"
    iso_path.parent.mkdir(parents=True, exist_ok=True)
    iso_path.write_bytes(b"fixture-iso\n")

    class FakeClient:
        def __init__(self) -> None:
            self.completed_sessions: list[str] = []
            self.copy_states = {
                f"{image_id}-1": {
                    "id": f"{image_id}-1",
                    "label_text": f"{image_id}-1",
                    "state": "lost",
                    "verification_state": "pending",
                    "location": None,
                },
                f"{image_id}-2": {
                    "id": f"{image_id}-2",
                    "label_text": f"{image_id}-2",
                    "state": "damaged",
                    "verification_state": "pending",
                    "location": None,
                },
                f"{image_id}-3": {
                    "id": f"{image_id}-3",
                    "label_text": f"{image_id}-3",
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
            }

        def _verified_count(self) -> int:
            return sum(
                1
                for copy in self.copy_states.values()
                if copy["state"] in {"registered", "verified"}
            )

        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": image_id,
                        "filename": f"{image_id}.iso",
                        "fill": 0.9,
                        "physical_copies_registered": self._verified_count(),
                        "physical_copies_required": 1,
                    }
                ],
            }

        def get_recovery_session(self, session_id: str) -> dict[str, object]:
            return {
                "id": session_id,
                "state": "expired",
                "latest_message": (
                    "Restored ISO data expired and was cleaned up; re-initiate recovery to "
                    "request a new restore."
                ),
                "images": [{"id": image_id, "filename": f"{image_id}.iso"}],
            }

        def get_recovery_session_for_image(self, image_id_arg: str) -> dict[str, object]:
            assert image_id_arg == image_id
            return self.get_recovery_session("rs-20260420T040001Z-1")

        def list_copies(self, image_id_arg: str) -> dict[str, object]:
            assert image_id_arg == image_id
            return {"copies": list(self.copy_states.values())}

        def download_recovered_iso(
            self,
            session_id: str,
            image_id_arg: str,
            output: Path,
        ) -> bytes:
            raise AssertionError("expired-session resume should not re-download ISO data")

        def register_copy(self, image_id_arg: str, location: str, *, copy_id: str | None = None):
            assert image_id_arg == image_id
            assert copy_id is not None
            self.copy_states[copy_id]["state"] = "registered"
            self.copy_states[copy_id]["location"] = location
            return {"copy": self.copy_states[copy_id]}

        def update_copy(
            self,
            image_id_arg: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            assert image_id_arg == image_id
            copy = self.copy_states[copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

        def complete_recovery_session(self, session_id: str) -> dict[str, object]:
            self.completed_sessions.append(session_id)
            return {"id": session_id, "state": "completed"}

    class FakeIsoVerifier:
        def verify(self, local_iso_path: Path) -> None:
            assert local_iso_path.read_bytes() == b"fixture-iso\n"

    class FakeBurner:
        def burn(self, local_iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert local_iso_path.read_bytes() == b"fixture-iso\n"
            assert copy_id == f"{image_id}-3"

    class FakeMediaVerifier:
        def verify(self, local_iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert local_iso_path.read_bytes() == b"fixture-iso\n"
            assert copy_id == f"{image_id}-3"

    class FakePrompts:
        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            assert (copy_id, device) == (f"{image_id}-3", "/dev/fake-sr0")

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            assert (copy_id, label_text) == (f"{image_id}-3", f"{image_id}-3")

        def prompt_location(self, copy_id: str) -> str:
            assert copy_id == f"{image_id}-3"
            return "vault-a/shelf-02"

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return True

    client = FakeClient()
    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: FakeBurner())
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: FakePrompts())

    result = runner.invoke(
        djdan_main.app,
        [
            "burn",
            "--device",
            "/dev/fake-sr0",
            "--staging-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "burn backlog cleared" in result.stdout
    assert (
        "restore window expired remotely; resuming from local staged ISO artifacts" in result.stderr
    )
    assert client.completed_sessions == ["rs-20260420T040001Z-1"]
    assert client.copy_states[f"{image_id}-3"]["state"] == "verified"
    assert not (tmp_path / image_id).exists()
    assert not (tmp_path / "burn-session.json").exists()


def test_djdan_recover_stages_all_pending_session_images_before_first_burn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_one = "20260420T040001Z"
    image_two = "20260420T040003Z"

    class FakeClient:
        def __init__(self) -> None:
            self.iso_downloads: list[str] = []
            self.copy_states = {
                image_one: {
                    f"{image_one}-3": {
                        "id": f"{image_one}-3",
                        "label_text": f"{image_one}-3",
                        "state": "needed",
                        "verification_state": "pending",
                        "location": None,
                    }
                },
                image_two: {
                    f"{image_two}-3": {
                        "id": f"{image_two}-3",
                        "label_text": f"{image_two}-3",
                        "state": "needed",
                        "verification_state": "pending",
                        "location": None,
                    }
                },
            }

        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": image_one,
                        "filename": f"{image_one}.iso",
                        "fill": 0.95,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 1,
                    },
                    {
                        "id": image_two,
                        "filename": f"{image_two}.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 1,
                    },
                ],
            }

        def get_recovery_session(self, session_id: str) -> dict[str, object]:
            return {
                "id": session_id,
                "state": "ready",
                "latest_message": "Restored ISO data is ready.",
                "images": [
                    {"id": image_one, "filename": f"{image_one}.iso"},
                    {"id": image_two, "filename": f"{image_two}.iso"},
                ],
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id in {image_one, image_two}
            return self.get_recovery_session("rs-20260420T040001Z-1")

        def list_copies(self, image_id: str) -> dict[str, object]:
            return {"copies": list(self.copy_states[image_id].values())}

        def download_recovered_iso(self, session_id: str, image_id: str, output: Path) -> bytes:
            assert session_id == "rs-20260420T040001Z-1"
            self.iso_downloads.append(image_id)
            output.write_bytes(f"{image_id}\n".encode())
            return output.read_bytes()

        def register_copy(self, image_id: str, location: str, *, copy_id: str | None = None):
            assert copy_id is not None
            copy = self.copy_states[image_id][copy_id]
            copy["state"] = "registered"
            copy["location"] = location
            return {"copy": copy}

        def update_copy(
            self,
            image_id: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            copy = self.copy_states[image_id][copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.is_file()

    class FakeBurner:
        def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.is_file()
            assert copy_id == f"{image_one}-3"

    class FakeMediaVerifier:
        def __init__(self) -> None:
            self.failed_once = False

        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.is_file()
            if not self.failed_once:
                self.failed_once = True
                raise RuntimeError(f"fixture burned-media verification failed for {copy_id}")

    class FakePrompts:
        def __init__(self) -> None:
            self.blank_waits = 0

        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            assert copy_id == f"{image_one}-3"
            self.blank_waits += 1
            if self.blank_waits > 1:
                raise RuntimeError("fresh blank media required after failed verification")

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            raise AssertionError("label confirmation should not run after verification failure")

        def prompt_location(self, copy_id: str) -> str:
            raise AssertionError("storage prompt should not run after verification failure")

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return True

    client = FakeClient()
    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: FakeBurner())
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: FakePrompts())

    result = runner.invoke(
        djdan_main.app,
        [
            "burn",
            "--device",
            "/dev/fake-sr0",
            "--staging-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert client.iso_downloads == [image_one, image_two]
    assert (tmp_path / image_one / f"{image_one}.iso").is_file()
    assert (tmp_path / image_two / f"{image_two}.iso").is_file()
    assert "discard or destroy this disc" in result.stderr
    assert "fresh blank media required after failed verification" in result.stderr


def test_djdan_burn_reports_recovery_handoffs_when_no_standard_backlog_exists(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeClient:
        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            assert (page, per_page, sort, order) == (1, 100, "finalized_at", "desc")
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 2,
                    }
                ],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "copies": [
                    {"id": "20260420T040001Z-1", "state": "lost"},
                    {"id": "20260420T040001Z-2", "state": "damaged"},
                    {"id": "20260420T040001Z-3", "state": "needed"},
                ]
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {
                "id": "rs-20260420T040001Z-1",
                "state": "restore_requested",
                "latest_message": (
                    "Archive restore requested; wait until the session is ready before "
                    "burning replacement media."
                ),
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)

    result = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
        input="\n",
    )

    assert result.exit_code == 0
    assert "burn backlog already clear" in result.stdout
    assert "burn backlog is waiting for image rebuild restore work" in result.stdout
    assert "rs-20260420T040001Z-1" in result.stdout
    assert "restore_requested" in result.stdout
    assert "Archive restore requested" in result.stdout


def test_djdan_burn_cleans_stale_completed_staging_when_backlog_is_clear(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_id = "20260420T040001Z"
    iso_dir = tmp_path / image_id
    iso_dir.mkdir()
    (iso_dir / f"{image_id}.iso").write_bytes(b"fixture-iso\n")

    state = djdan_main.BurnSessionState.load(djdan_main._burn_state_path(tmp_path))
    for copy_id, location in {
        f"{image_id}-1": "vault-a/shelf-01",
        f"{image_id}-2": "vault-b/shelf-01",
    }.items():
        progress = state.copy_progress(image_id, copy_id)
        progress.burned = True
        progress.media_verified = True
        progress.label_confirmed = True
        progress.location = location
    state.image_progress(image_id).verified_sha256 = "fixture-sha256"
    state.save()

    class FakeClient:
        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": image_id,
                        "filename": f"{image_id}.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 2,
                        "physical_copies_required": 2,
                    }
                ],
            }

        def list_copies(self, requested_image_id: str) -> dict[str, object]:
            assert requested_image_id == image_id
            return {
                "copies": [
                    {
                        "id": f"{image_id}-1",
                        "state": "verified",
                        "verification_state": "verified",
                        "location": "vault-a/shelf-01",
                    },
                    {
                        "id": f"{image_id}-2",
                        "state": "verified",
                        "verification_state": "verified",
                        "location": "vault-b/shelf-01",
                    },
                ]
            }

    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", object)
    monkeypatch.setattr(djdan_main, "build_disc_burner", object)
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", object)
    monkeypatch.setattr(djdan_main, "build_burn_prompts", object)

    result = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "burn backlog already clear" in result.stdout
    assert f"cleared staged ISO artifacts for {image_id}" in result.stderr
    assert not iso_dir.exists()
    assert not (tmp_path / "burn-session.json").exists()


@pytest.mark.parametrize(
    ("platform", "expected_device"),
    [
        ("darwin", "default"),
        ("linux", "/dev/sr0"),
    ],
)
def test_djdan_burn_without_device_uses_platform_default(
    monkeypatch,
    tmp_path: Path,
    platform: str,
    expected_device: str,
) -> None:
    seen_devices: list[str] = []
    discover_calls = 0

    class FakeClient:
        pass

    def fake_discover_burn_backlog(client, session_state=None, *, staging_dir=None):
        nonlocal discover_calls
        discover_calls += 1
        if discover_calls == 1:
            return [
                djdan_main.BurnBacklogItem(
                    image_id="20260420T040001Z",
                    candidate_id=None,
                    filename="20260420T040001Z.iso",
                    fill=0.9,
                )
            ]
        return []

    def fake_process_burn_backlog_item(
        item,
        *,
        client,
        staging_dir,
        session_state,
        iso_verifier,
        burner,
        media_verifier,
        prompts,
        device,
        simulate=False,
    ):
        seen_devices.append(device)
        return [f"{item.image_id}-1"]

    monkeypatch.setattr(djdan_main.sys, "platform", platform)
    monkeypatch.setattr(djdan_main, "ApiClient", FakeClient)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", object)
    monkeypatch.setattr(djdan_main, "build_disc_burner", object)
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", object)
    monkeypatch.setattr(djdan_main, "build_burn_prompts", object)
    monkeypatch.setattr(djdan_main, "_discover_burn_backlog", fake_discover_burn_backlog)
    monkeypatch.setattr(
        djdan_main,
        "_process_burn_backlog_item",
        fake_process_burn_backlog_item,
    )
    monkeypatch.setattr(djdan_main, "_discover_recovery_handoffs", lambda client: [])

    result = runner.invoke(
        djdan_main.app,
        ["burn", "--staging-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert seen_devices == [expected_device]
    assert "burn backlog cleared" in result.stdout


def test_djdan_burn_resumes_registered_copy_verification_update(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_id = "20260420T040001Z"
    copy_id = f"{image_id}-2"

    class FakeClient:
        def __init__(self) -> None:
            self.copy_states = {
                f"{image_id}-1": {
                    "id": f"{image_id}-1",
                    "label_text": f"{image_id}-1",
                    "state": "verified",
                    "verification_state": "verified",
                    "location": "Shelf A",
                },
                copy_id: {
                    "id": copy_id,
                    "label_text": copy_id,
                    "state": "registered",
                    "verification_state": "pending",
                    "location": "Shelf B",
                },
            }
            self.updated: list[tuple[str, str, str | None, str | None, str | None]] = []

        def get_plan(self, *, page: int, per_page: int, sort: str, order: str, iso_ready: bool):
            return {"page": 1, "pages": 0, "candidates": []}

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            verified = sum(
                1 for copy in self.copy_states.values() if copy["verification_state"] == "verified"
            )
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": image_id,
                        "filename": f"{image_id}.iso",
                        "fill": 0.9,
                        "physical_copies_registered": 2,
                        "physical_copies_required": 2,
                        "physical_copies_verified": verified,
                    }
                ],
            }

        def list_copies(self, requested_image_id: str) -> dict[str, object]:
            assert requested_image_id == image_id
            return {"copies": list(self.copy_states.values())}

        def update_copy(
            self,
            requested_image_id: str,
            requested_copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ) -> dict[str, object]:
            self.updated.append(
                (requested_image_id, requested_copy_id, location, state, verification_state)
            )
            copy = self.copy_states[requested_copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return copy

    state = djdan_main.BurnSessionState.load(djdan_main._burn_state_path(tmp_path))
    progress = state.copy_progress(image_id, copy_id)
    progress.burned = True
    progress.media_verified = True
    progress.label_confirmed = True
    progress.location = "Shelf B"
    state.save()

    client = FakeClient()
    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: object())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: object())
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: object())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: object())

    result = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert client.updated == [
        (image_id, copy_id, "Shelf B", "verified", "verified"),
    ]
    assert copy_id in result.stdout


def test_djdan_burn_simulate_uses_dummy_burn_without_registration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    image_id = "20260420T040001Z"
    copy_id = f"{image_id}-1"

    class FakeClient:
        def __init__(self) -> None:
            self.finalized = False
            self.iso_bytes = b"fixture-iso\n"
            self.register_calls: list[str] = []

        def get_plan(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            iso_ready: bool,
        ) -> dict[str, object]:
            if self.finalized:
                return {"page": 1, "pages": 0, "candidates": []}
            return {
                "page": 1,
                "pages": 1,
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
                        "fill": 0.9,
                        "iso_ready": True,
                    }
                ],
            }

        def list_images(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
        ) -> dict[str, object]:
            return {"page": 1, "pages": 0, "images": []}

        def finalize_image(self, candidate_id: str) -> dict[str, object]:
            assert candidate_id == "img_2026-04-20_01"
            self.finalized = True
            return {"id": image_id, "filename": f"{image_id}.iso"}

        def list_copies(self, current_image_id: str) -> dict[str, object]:
            assert current_image_id == image_id
            return {
                "copies": [
                    {
                        "id": copy_id,
                        "label_text": copy_id,
                        "state": "needed",
                        "verification_state": "pending",
                        "location": None,
                    }
                ]
            }

        def download_iso(self, current_image_id: str, output: Path) -> bytes:
            assert current_image_id == image_id
            output.write_bytes(self.iso_bytes)
            return self.iso_bytes

        def register_copy(
            self,
            current_image_id: str,
            location: str,
            *,
            copy_id: str | None = None,
        ):
            self.register_calls.append(str(copy_id))
            raise AssertionError("simulated burn must not register copies")

        def update_copy(self, *args, **kwargs):
            raise AssertionError("simulated burn must not update copies")

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    commands: list[list[str]] = []

    def fake_run(command, *, capture_output, text, check):
        commands.append(command)
        return djdan_main.subprocess.CompletedProcess(command, 0, "", "")

    client = FakeClient()
    monkeypatch.setattr(djdan_main.sys, "platform", "linux")
    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(djdan_main.subprocess, "run", fake_run)

    result = runner.invoke(
        djdan_main.app,
        ["burn", "--simulate", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
        input="\n",
    )

    assert result.exit_code == 0
    assert "simulated burn completed; no copies were registered" in result.stdout
    assert copy_id in result.stdout
    assert "simulating burn copy 20260420T040001Z-1" in result.stderr
    assert "verifying burned media" not in result.stderr
    assert "label text:" not in result.stderr
    assert client.register_calls == []
    assert commands == [
        [
            "/usr/bin/xorriso",
            "-as",
            "cdrecord",
            "-v",
            "-dummy",
            "dev=/dev/fake-sr0",
            str(tmp_path / image_id / f"{image_id}.iso"),
        ]
    ]


def test_djdan_burn_waits_for_label_confirmation_before_registration_and_resumes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    copy_one = "20260420T040001Z-1"
    copy_two = "20260420T040001Z-2"

    class FakeClient:
        def __init__(self) -> None:
            self.finalized = False
            self.iso_bytes = b"fixture-iso\n"
            self.register_calls: list[str] = []
            self.copy_states = {
                copy_one: {
                    "id": copy_one,
                    "label_text": copy_one,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
                copy_two: {
                    "id": copy_two,
                    "label_text": copy_two,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
            }

        def _registered_count(self) -> int:
            return sum(
                1
                for copy in self.copy_states.values()
                if copy["state"] in {"registered", "verified"}
            )

        def get_plan(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            iso_ready: bool,
        ) -> dict[str, object]:
            if self.finalized:
                return {"page": 1, "pages": 0, "candidates": []}
            return {
                "page": 1,
                "pages": 1,
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
                        "fill": 0.9,
                        "iso_ready": True,
                    }
                ],
            }

        def list_images(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
        ) -> dict[str, object]:
            if not self.finalized:
                return {"page": 1, "pages": 0, "images": []}
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": self._registered_count(),
                        "physical_copies_required": 2,
                    }
                ],
            }

        def finalize_image(self, candidate_id: str) -> dict[str, object]:
            assert candidate_id == "img_2026-04-20_01"
            self.finalized = True
            return {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"}

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {"copies": list(self.copy_states.values())}

        def download_iso(self, image_id: str, output: Path) -> bytes:
            assert image_id == "20260420T040001Z"
            output.write_bytes(self.iso_bytes)
            return self.iso_bytes

        def register_copy(self, image_id: str, location: str, *, copy_id: str | None = None):
            assert image_id == "20260420T040001Z"
            assert copy_id is not None
            self.register_calls.append(copy_id)
            self.copy_states[copy_id]["state"] = "registered"
            self.copy_states[copy_id]["location"] = location
            return {"copy": self.copy_states[copy_id]}

        def update_copy(
            self,
            image_id: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            assert image_id == "20260420T040001Z"
            copy = self.copy_states[copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakeBurner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.read_bytes() == b"fixture-iso\n"
            self.calls.append(copy_id)

    class FakeMediaVerifier:
        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakePrompts:
        def __init__(self) -> None:
            self.confirmed: set[str] = set()
            self.available: set[str] = set()
            self.locations = {
                copy_one: "vault-a/shelf-01",
                copy_two: "vault-b/shelf-01",
            }

        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            assert device == "/dev/fake-sr0"

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            assert label_text == copy_id
            if copy_id not in self.confirmed:
                raise RuntimeError(f"label confirmation required for {copy_id}")

        def prompt_location(self, copy_id: str) -> str:
            return self.locations[copy_id]

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return copy_id in self.available

    client = FakeClient()
    burner = FakeBurner()
    prompts = FakePrompts()

    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert first.exit_code == 1
    assert f"error: label confirmation required for {copy_one}" in first.stderr
    assert client.register_calls == []
    assert burner.calls == [copy_one]
    assert client.copy_states[copy_one]["state"] == "needed"

    prompts.confirmed.update({copy_one, copy_two})
    prompts.available.update({copy_one, copy_two})
    second = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "resuming label confirmation for 20260420T040001Z-1" in second.stderr
    assert "burning copy 20260420T040001Z-1" not in second.stderr
    assert burner.calls == [copy_one, copy_two]
    assert client.register_calls == [copy_one, copy_two]
    assert client.copy_states[copy_one]["state"] == "verified"
    assert client.copy_states[copy_two]["verification_state"] == "verified"
    assert "cleared staged ISO artifacts for 20260420T040001Z" in second.stderr
    assert not (tmp_path / "20260420T040001Z").exists()
    assert not (tmp_path / "burn-session.json").exists()


def test_djdan_burn_retries_same_run_after_failed_native_media_verification(
    monkeypatch,
    tmp_path: Path,
) -> None:
    copy_one = "20260420T040001Z-1"
    copy_two = "20260420T040001Z-2"

    class FakeClient:
        def __init__(self) -> None:
            self.finalized = False
            self.iso_bytes = b"fixture-iso\n"
            self.register_calls: list[str] = []
            self.copy_states = {
                copy_one: {
                    "id": copy_one,
                    "label_text": copy_one,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
                copy_two: {
                    "id": copy_two,
                    "label_text": copy_two,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
            }

        def _registered_count(self) -> int:
            return sum(
                1
                for copy in self.copy_states.values()
                if copy["state"] in {"registered", "verified"}
            )

        def get_plan(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            iso_ready: bool,
        ) -> dict[str, object]:
            if self.finalized:
                return {"page": 1, "pages": 0, "candidates": []}
            return {
                "page": 1,
                "pages": 1,
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
                        "fill": 0.9,
                        "iso_ready": True,
                    }
                ],
            }

        def list_images(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
        ) -> dict[str, object]:
            if not self.finalized:
                return {"page": 1, "pages": 0, "images": []}
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": self._registered_count(),
                        "physical_copies_required": 2,
                    }
                ],
            }

        def finalize_image(self, candidate_id: str) -> dict[str, object]:
            assert candidate_id == "img_2026-04-20_01"
            self.finalized = True
            return {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"}

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {"copies": list(self.copy_states.values())}

        def download_iso(self, image_id: str, output: Path) -> bytes:
            assert image_id == "20260420T040001Z"
            output.write_bytes(self.iso_bytes)
            return self.iso_bytes

        def register_copy(self, image_id: str, location: str, *, copy_id: str | None = None):
            assert image_id == "20260420T040001Z"
            assert copy_id is not None
            self.register_calls.append(copy_id)
            self.copy_states[copy_id]["state"] = "registered"
            self.copy_states[copy_id]["location"] = location
            return {"copy": self.copy_states[copy_id]}

        def update_copy(
            self,
            image_id: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            assert image_id == "20260420T040001Z"
            copy = self.copy_states[copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakeBurner:
        verifies_media = True

        def __init__(self) -> None:
            self.fail_once_copy_ids = {copy_one}
            self.calls: list[str] = []

        def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.read_bytes() == b"fixture-iso\n"
            self.calls.append(copy_id)
            if copy_id in self.fail_once_copy_ids:
                self.fail_once_copy_ids.remove(copy_id)
                raise djdan_main.BurnedMediaVerificationError(
                    f"fixture native verification failed for {copy_id}"
                )

    class FakeMediaVerifier:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.read_bytes() == b"fixture-iso\n"
            self.calls.append(copy_id)

    class FakePrompts:
        def __init__(self) -> None:
            self.confirmed: set[str] = set()
            self.available: set[str] = set()
            self.blank_waits: list[str] = []
            self.locations = {
                copy_one: "vault-a/shelf-01",
                copy_two: "vault-b/shelf-01",
            }

        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            assert device == "/dev/fake-sr0"
            self.blank_waits.append(copy_id)

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            assert label_text == copy_id
            if copy_id not in self.confirmed:
                raise RuntimeError(f"label confirmation required for {copy_id}")

        def prompt_location(self, copy_id: str) -> str:
            return self.locations[copy_id]

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return copy_id in self.available

    client = FakeClient()
    burner = FakeBurner()
    media_verifier = FakeMediaVerifier()
    prompts = FakePrompts()

    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(
        djdan_main,
        "build_burned_media_verifier",
        lambda: media_verifier,
    )
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: prompts)

    prompts.confirmed.update({copy_one, copy_two})
    result = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert f"burned media verification failed for {copy_one}" in result.stderr
    assert "discard or destroy this disc" in result.stderr
    assert f"Insert a new blank disc to retry burn copy {copy_one}" in result.stderr
    assert result.stderr.count("burning copy 20260420T040001Z-1") == 2
    assert prompts.blank_waits == [copy_one, copy_one, copy_two]
    assert burner.calls == [copy_one, copy_one, copy_two]
    assert media_verifier.calls == []
    assert client.register_calls == [copy_one, copy_two]
    assert client.copy_states[copy_one]["state"] == "verified"
    assert client.copy_states[copy_two]["verification_state"] == "verified"


def test_djdan_burn_redownloads_invalid_staged_iso(monkeypatch, tmp_path: Path) -> None:
    copy_one = "20260420T040001Z-1"
    copy_two = "20260420T040001Z-2"
    events: list[str] = []

    class FakeClient:
        def __init__(self) -> None:
            self.finalized = False
            self.iso_bytes = b"fixture-iso\n"
            self.download_calls = 0
            self.copy_states = {
                copy_one: {
                    "id": copy_one,
                    "label_text": copy_one,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
                copy_two: {
                    "id": copy_two,
                    "label_text": copy_two,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
            }

        def _registered_count(self) -> int:
            return sum(
                1
                for copy in self.copy_states.values()
                if copy["state"] in {"registered", "verified"}
            )

        def get_plan(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            iso_ready: bool,
        ) -> dict[str, object]:
            if self.finalized:
                return {"page": 1, "pages": 0, "candidates": []}
            return {
                "page": 1,
                "pages": 1,
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
                        "fill": 0.9,
                        "iso_ready": True,
                    }
                ],
            }

        def list_images(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
        ) -> dict[str, object]:
            if not self.finalized:
                return {"page": 1, "pages": 0, "images": []}
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": self._registered_count(),
                        "physical_copies_required": 2,
                    }
                ],
            }

        def finalize_image(self, candidate_id: str) -> dict[str, object]:
            assert candidate_id == "img_2026-04-20_01"
            self.finalized = True
            return {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"}

        def list_copies(self, image_id: str) -> dict[str, object]:
            assert image_id == "20260420T040001Z"
            return {"copies": list(self.copy_states.values())}

        def download_iso(self, image_id: str, output: Path) -> bytes:
            assert image_id == "20260420T040001Z"
            self.download_calls += 1
            events.append("download")
            output.write_bytes(self.iso_bytes)
            return self.iso_bytes

        def register_copy(self, image_id: str, location: str, *, copy_id: str | None = None):
            assert image_id == "20260420T040001Z"
            assert copy_id is not None
            self.copy_states[copy_id]["state"] = "registered"
            self.copy_states[copy_id]["location"] = location
            return {"copy": self.copy_states[copy_id]}

        def update_copy(
            self,
            image_id: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            copy = self.copy_states[copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakeBurner:
        def __init__(self) -> None:
            self.fail_copy_ids = {copy_two}

        def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert iso_path.exists()
            events.append(f"burn:{copy_id}")
            if copy_id in self.fail_copy_ids:
                raise RuntimeError(f"fixture burn failed for {copy_id}")

    class FakeMediaVerifier:
        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakePrompts:
        def __init__(self) -> None:
            self.confirmed = {copy_one}
            self.available = {copy_two}
            self.locations = {
                copy_one: "vault-a/shelf-01",
                copy_two: "vault-b/shelf-01",
            }

        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            assert device == "/dev/fake-sr0"
            events.append(f"blank:{copy_id}")

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            if copy_id not in self.confirmed:
                raise RuntimeError(f"label confirmation required for {copy_id}")

        def prompt_location(self, copy_id: str) -> str:
            return self.locations[copy_id]

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return copy_id in self.available

    client = FakeClient()
    burner = FakeBurner()
    prompts = FakePrompts()

    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert first.exit_code == 1
    assert "fixture burn failed for 20260420T040001Z-2" in first.stderr
    assert client.download_calls == 1
    assert events == [
        f"blank:{copy_one}",
        "download",
        f"burn:{copy_one}",
        f"blank:{copy_two}",
        f"burn:{copy_two}",
    ]

    staged_iso = tmp_path / "20260420T040001Z" / "20260420T040001Z.iso"
    staged_iso.write_bytes(b"corrupted-iso\n")
    burner.fail_copy_ids.clear()
    prompts.confirmed.add(copy_two)
    prompts.available.add(copy_two)

    second = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "staged ISO is invalid" in second.stderr
    assert "re-downloading" in second.stderr
    assert client.download_calls == 2
    assert events[-3:] == [f"blank:{copy_two}", "download", f"burn:{copy_two}"]
    assert client.copy_states[copy_two]["state"] == "verified"


def test_djdan_burn_reburns_when_unlabeled_disc_is_unavailable_on_resume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    copy_one = "20260420T040001Z-1"
    copy_two = "20260420T040001Z-2"

    class FakeClient:
        def __init__(self) -> None:
            self.finalized = False
            self.iso_bytes = b"fixture-iso\n"
            self.register_calls: list[str] = []
            self.copy_states = {
                copy_one: {
                    "id": copy_one,
                    "label_text": copy_one,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
                copy_two: {
                    "id": copy_two,
                    "label_text": copy_two,
                    "state": "needed",
                    "verification_state": "pending",
                    "location": None,
                },
            }

        def _registered_count(self) -> int:
            return sum(
                1
                for copy in self.copy_states.values()
                if copy["state"] in {"registered", "verified"}
            )

        def get_plan(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
            iso_ready: bool,
        ) -> dict[str, object]:
            if self.finalized:
                return {"page": 1, "pages": 0, "candidates": []}
            return {
                "page": 1,
                "pages": 1,
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
                        "fill": 0.9,
                        "iso_ready": True,
                    }
                ],
            }

        def list_images(
            self,
            *,
            page: int,
            per_page: int,
            sort: str,
            order: str,
        ) -> dict[str, object]:
            if not self.finalized:
                return {"page": 1, "pages": 0, "images": []}
            return {
                "page": 1,
                "pages": 1,
                "images": [
                    {
                        "id": "20260420T040001Z",
                        "filename": "20260420T040001Z.iso",
                        "fill": 0.9,
                        "physical_copies_registered": self._registered_count(),
                        "physical_copies_required": 2,
                    }
                ],
            }

        def finalize_image(self, candidate_id: str) -> dict[str, object]:
            self.finalized = True
            return {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"}

        def list_copies(self, image_id: str) -> dict[str, object]:
            return {"copies": list(self.copy_states.values())}

        def download_iso(self, image_id: str, output: Path) -> bytes:
            output.write_bytes(self.iso_bytes)
            return self.iso_bytes

        def register_copy(self, image_id: str, location: str, *, copy_id: str | None = None):
            assert copy_id is not None
            self.register_calls.append(copy_id)
            self.copy_states[copy_id]["state"] = "registered"
            self.copy_states[copy_id]["location"] = location
            return {"copy": self.copy_states[copy_id]}

        def update_copy(
            self,
            image_id: str,
            copy_id: str,
            *,
            location: str | None = None,
            state: str | None = None,
            verification_state: str | None = None,
        ):
            copy = self.copy_states[copy_id]
            if location is not None:
                copy["location"] = location
            if state is not None:
                copy["state"] = state
            if verification_state is not None:
                copy["verification_state"] = verification_state
            return {"copy": copy}

    class FakeIsoVerifier:
        def verify(self, iso_path: Path) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakeBurner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            self.calls.append(copy_id)

    class FakeMediaVerifier:
        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert iso_path.read_bytes() == b"fixture-iso\n"

    class FakePrompts:
        def __init__(self) -> None:
            self.confirmed: set[str] = set()
            self.available: set[str] = set()
            self.locations = {
                copy_one: "vault-a/shelf-01",
                copy_two: "vault-b/shelf-01",
            }

        def wait_for_blank_disc(
            self, copy_id: str, *, device: str, target_bytes: int | None = None
        ) -> None:
            return None

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            if copy_id not in self.confirmed:
                raise RuntimeError(f"label confirmation required for {copy_id}")

        def prompt_location(self, copy_id: str) -> str:
            return self.locations[copy_id]

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return copy_id in self.available

    client = FakeClient()
    burner = FakeBurner()
    prompts = FakePrompts()

    monkeypatch.setattr(djdan_main, "ApiClient", lambda: client)
    monkeypatch.setattr(djdan_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(djdan_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(djdan_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(djdan_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert first.exit_code == 1
    assert burner.calls == [copy_one]

    prompts.confirmed.update({copy_one, copy_two})
    prompts.available.add(copy_two)
    second = runner.invoke(
        djdan_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "unlabeled disc for 20260420T040001Z-1 is unavailable; restarting burn" in second.stderr
    assert burner.calls == [copy_one, copy_one, copy_two]
    assert client.register_calls == [copy_one, copy_two]
