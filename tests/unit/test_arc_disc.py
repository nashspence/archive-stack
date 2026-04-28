from __future__ import annotations

import hashlib
from pathlib import Path

from typer.testing import CliRunner

import arc_disc.main as arc_disc_main
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


def test_arc_disc_fetch_recovers_in_memory_and_reports_progress(monkeypatch) -> None:
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

    monkeypatch.setattr(arc_disc_main, "ApiClient", FakeClient)
    monkeypatch.setattr(arc_disc_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        arc_disc_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n",
    )

    assert result.exit_code == 0
    assert '"state": "done"' in result.stdout
    assert "20260420T040001Z-1" in result.stderr
    assert "current file" in result.stderr
    assert "manifest" in result.stderr
    assert "/s" in result.stderr
    assert uploaded == [
        ("https://uploads.test/fx-1/e1", 0, "sha256", recovered[:8]),
        ("https://uploads.test/fx-1/e1", 8, "sha256", recovered[8:]),
    ]


def test_arc_disc_fetch_reports_clean_error_when_optical_read_fails(monkeypatch) -> None:
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

    monkeypatch.setattr(arc_disc_main, "ApiClient", FakeClient)
    monkeypatch.setattr(arc_disc_main, "build_optical_reader", lambda: FailingReader())

    result = runner.invoke(
        arc_disc_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0"],
        input="\n",
    )

    assert result.exit_code == 1
    assert (
        "error: fixture optical read failed for disc/000001.bin on /dev/fake-sr0" in result.stderr
    )
    assert "Traceback" not in result.stderr


def test_arc_disc_fetch_resumes_split_entry_from_session_offset(monkeypatch) -> None:
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

    monkeypatch.setattr(arc_disc_main, "ApiClient", FakeClient)
    monkeypatch.setattr(arc_disc_main, "build_optical_reader", lambda: FakeReader())

    result = runner.invoke(
        arc_disc_main.app,
        ["fetch", "fx-1", "--device", "/dev/fake-sr0", "--json"],
        input="\n",
    )

    assert result.exit_code == 0
    assert '"state": "done"' in result.stdout
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
                "candidates": [
                    {
                        "candidate_id": "img_2026-04-20_01",
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

    backlog = arc_disc_main._discover_burn_backlog(FakeClient())

    assert [(item.candidate_id, item.image_id) for item in backlog] == [
        ("img_2026-04-20_01", None),
        (None, "20260420T040003Z"),
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

    assert arc_disc_main._discover_burn_backlog(FakeClient()) == []


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
                "state": "pending_approval",
                "latest_message": (
                    "Approve the estimated restore cost before Riverhog requests archive "
                    "restore."
                ),
            }

    assert arc_disc_main._discover_recovery_handoffs(FakeClient()) == [
        arc_disc_main.RecoveryHandoff(
            image_id="20260420T040001Z",
            session_id="rs-20260420T040001Z-1",
            state="pending_approval",
            latest_message=(
                "Approve the estimated restore cost before Riverhog requests archive "
                "restore."
            ),
        )
    ]


def test_discover_active_recovery_sessions_dedupes_multi_image_sessions() -> None:
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
                    },
                    {
                        "id": "20260420T040003Z",
                        "filename": "20260420T040003Z.iso",
                        "fill": 0.7,
                        "physical_copies_registered": 0,
                        "physical_copies_required": 2,
                    },
                ],
            }

        def list_copies(self, image_id: str) -> dict[str, object]:
            return {
                "copies": [
                    {"id": f"{image_id}-1", "state": "lost"},
                    {"id": f"{image_id}-2", "state": "damaged"},
                    {"id": f"{image_id}-3", "state": "needed"},
                ]
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            assert image_id in {"20260420T040001Z", "20260420T040003Z"}
            return {
                "id": "rs-20260420T040001Z-1",
                "state": "pending_approval",
                "latest_message": (
                    "Approve the estimated restore cost before Riverhog requests archive "
                    "restore."
                ),
                "images": [
                    {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                    {"id": "20260420T040003Z", "filename": "20260420T040003Z.iso"},
                ],
            }

    sessions = arc_disc_main._discover_active_recovery_sessions(FakeClient())

    assert sessions == [
        arc_disc_main.RecoverySessionHint(
            session_id="rs-20260420T040001Z-1",
            state="pending_approval",
            latest_message=(
                "Approve the estimated restore cost before Riverhog requests archive "
                "restore."
            ),
            images=(
                arc_disc_main.RecoverySessionImageHint(
                    image_id="20260420T040001Z",
                    filename="20260420T040001Z.iso",
                ),
                arc_disc_main.RecoverySessionImageHint(
                    image_id="20260420T040003Z",
                    filename="20260420T040003Z.iso",
                ),
            ),
        )
    ]


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

    assert not arc_disc_main._is_standard_burn_backlog_image(
        FakeClient(),
        "20260420T040001Z",
    )


def test_arc_disc_recover_lists_active_sessions(monkeypatch) -> None:
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
            return {
                "copies": [
                    {"id": "20260420T040001Z-1", "state": "lost"},
                    {"id": "20260420T040001Z-2", "state": "damaged"},
                    {"id": "20260420T040001Z-3", "state": "needed"},
                ]
            }

        def get_recovery_session_for_image(self, image_id: str) -> dict[str, object]:
            return {
                "id": "rs-20260420T040001Z-1",
                "state": "pending_approval",
                "latest_message": (
                    "Approve the estimated restore cost before Riverhog requests archive "
                    "restore."
                ),
                "images": [
                    {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                ],
            }

    monkeypatch.setattr(arc_disc_main, "ApiClient", FakeClient)

    result = runner.invoke(arc_disc_main.app, ["recover"])

    assert result.exit_code == 0
    assert "rs-20260420T040001Z-1" in result.stdout
    assert "pending_approval" in result.stdout
    assert "20260420T040001Z" in result.stdout


def test_arc_disc_recover_approves_waiting_session(monkeypatch, tmp_path: Path) -> None:
    class FakeClient:
        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {"page": 1, "pages": 0, "images": []}

        def get_recovery_session(self, session_id: str) -> dict[str, object]:
            assert session_id == "rs-20260420T040001Z-1"
            return {
                "id": session_id,
                "state": "pending_approval",
                "latest_message": (
                    "Approve the estimated restore cost before Riverhog requests archive "
                    "restore."
                ),
                "images": [
                    {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                ],
            }

        def approve_recovery_session(self, session_id: str) -> dict[str, object]:
            assert session_id == "rs-20260420T040001Z-1"
            return {
                "id": session_id,
                "state": "restore_requested",
                "latest_message": (
                    "Archive restore requested; wait for the ready notification before "
                    "downloading or burning replacement media."
                ),
                "images": [
                    {"id": "20260420T040001Z", "filename": "20260420T040001Z.iso"},
                ],
            }

    monkeypatch.setattr(arc_disc_main, "ApiClient", FakeClient)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: object())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: object())
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: object())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: object())

    result = runner.invoke(
        arc_disc_main.app,
        ["recover", "rs-20260420T040001Z-1", "--staging-dir", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "recovery session rs-20260420T040001Z-1 is restore_requested" in result.stdout
    assert "Archive restore requested" in result.stdout


def test_arc_disc_recover_ready_session_burns_replacements_and_cleans_staging(
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

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {"page": 1, "pages": 0, "images": []}

        def get_recovery_session(self, session_id: str) -> dict[str, object]:
            return {
                "id": session_id,
                "state": "ready",
                "latest_message": "Restored ISO data is ready.",
                "images": [{"id": image_id, "filename": f"{image_id}.iso"}],
            }

        def list_copies(self, image_id_arg: str) -> dict[str, object]:
            assert image_id_arg == image_id
            self._ensure_followup_copy()
            return {"copies": list(self.copy_states.values())}

        def download_iso(self, image_id_arg: str, output: Path) -> bytes:
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

        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
            assert device == "/dev/fake-sr0"

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            assert label_text == copy_id

        def prompt_location(self, copy_id: str) -> str:
            return self.locations[copy_id]

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return True

    client = FakeClient()
    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: FakeBurner())
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: FakePrompts())

    result = runner.invoke(
        arc_disc_main.app,
        [
            "recover",
            "rs-20260420T040001Z-1",
            "--device",
            "/dev/fake-sr0",
            "--staging-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "recovery session rs-20260420T040001Z-1 completed" in result.stdout
    assert f"{image_id}-3" in result.stdout
    assert f"{image_id}-4" in result.stdout
    assert client.completed_sessions == ["rs-20260420T040001Z-1"]
    assert client.copy_states[f"{image_id}-3"]["state"] == "verified"
    assert client.copy_states[f"{image_id}-4"]["state"] == "verified"
    assert not (tmp_path / image_id).exists()
    assert not (tmp_path / "burn-session.json").exists()


def test_arc_disc_recover_can_finish_expired_session_from_local_staging(
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

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {"page": 1, "pages": 0, "images": []}

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

        def list_copies(self, image_id_arg: str) -> dict[str, object]:
            assert image_id_arg == image_id
            return {"copies": list(self.copy_states.values())}

        def download_iso(self, image_id_arg: str, output: Path) -> bytes:
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
        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
            assert (copy_id, device) == (f"{image_id}-3", "/dev/fake-sr0")

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            assert (copy_id, label_text) == (f"{image_id}-3", f"{image_id}-3")

        def prompt_location(self, copy_id: str) -> str:
            assert copy_id == f"{image_id}-3"
            return "vault-a/shelf-02"

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return True

    client = FakeClient()
    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: FakeBurner())
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: FakePrompts())

    result = runner.invoke(
        arc_disc_main.app,
        [
            "recover",
            "rs-20260420T040001Z-1",
            "--device",
            "/dev/fake-sr0",
            "--staging-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "recovery session rs-20260420T040001Z-1 completed" in result.stdout
    assert (
        "restore window expired remotely; resuming from local staged ISO artifacts"
        in result.stderr
    )
    assert client.completed_sessions == ["rs-20260420T040001Z-1"]
    assert client.copy_states[f"{image_id}-3"]["state"] == "verified"
    assert not (tmp_path / image_id).exists()
    assert not (tmp_path / "burn-session.json").exists()


def test_arc_disc_recover_stages_all_pending_session_images_before_first_burn(
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

        def list_images(self, *, page: int, per_page: int, sort: str, order: str):
            return {"page": 1, "pages": 0, "images": []}

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

        def list_copies(self, image_id: str) -> dict[str, object]:
            return {"copies": list(self.copy_states[image_id].values())}

        def download_iso(self, image_id: str, output: Path) -> bytes:
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
        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.is_file()
            raise RuntimeError(f"fixture burned-media verification failed for {copy_id}")

    class FakePrompts:
        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
            assert copy_id == f"{image_one}-3"

        def confirm_label(self, copy_id: str, *, label_text: str) -> None:
            raise AssertionError("label confirmation should not run after verification failure")

        def prompt_location(self, copy_id: str) -> str:
            raise AssertionError("storage prompt should not run after verification failure")

        def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
            return True

    client = FakeClient()
    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: FakeBurner())
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: FakePrompts())

    result = runner.invoke(
        arc_disc_main.app,
        [
            "recover",
            "rs-20260420T040001Z-1",
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


def test_arc_disc_burn_reports_recovery_handoffs_when_no_standard_backlog_exists(
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
                "state": "pending_approval",
                "latest_message": (
                    "Approve the estimated restore cost before Riverhog requests archive "
                    "restore."
                ),
            }

    monkeypatch.setattr(arc_disc_main, "ApiClient", FakeClient)

    result = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
        input="\n",
    )

    assert result.exit_code == 0
    assert "burn backlog already clear" in result.stdout
    assert "Glacier recovery work remains" in result.stdout
    assert "rs-20260420T040001Z-1" in result.stdout
    assert "pending_approval" in result.stdout
    assert "Approve the estimated restore cost" in result.stdout


def test_arc_disc_burn_waits_for_label_confirmation_before_registration_and_resumes(
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

        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
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

    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        arc_disc_main.app,
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
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "resuming label confirmation for 20260420T040001Z-1" in second.stderr
    assert "burning copy 20260420T040001Z-1" not in second.stderr
    assert burner.calls == [copy_one, copy_two]
    assert client.register_calls == [copy_one, copy_two]
    assert client.copy_states[copy_one]["state"] == "verified"
    assert client.copy_states[copy_two]["verification_state"] == "verified"


def test_arc_disc_burn_resumes_from_media_verification_when_unfinished_disc_is_available(
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
        def __init__(self) -> None:
            self.fail_copy_ids = {copy_one}
            self.calls: list[str] = []

        def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
            assert device == "/dev/fake-sr0"
            assert iso_path.read_bytes() == b"fixture-iso\n"
            self.calls.append(copy_id)
            if copy_id in self.fail_copy_ids:
                raise RuntimeError(f"fixture burned-media verification failed for {copy_id}")

    class FakePrompts:
        def __init__(self) -> None:
            self.confirmed: set[str] = set()
            self.available: set[str] = set()
            self.locations = {
                copy_one: "vault-a/shelf-01",
                copy_two: "vault-b/shelf-01",
            }

        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
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
    media_verifier = FakeMediaVerifier()
    prompts = FakePrompts()

    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(
        arc_disc_main,
        "build_burned_media_verifier",
        lambda: media_verifier,
    )
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert first.exit_code == 1
    assert f"error: fixture burned-media verification failed for {copy_one}" in first.stderr
    assert burner.calls == [copy_one]
    assert media_verifier.calls == [copy_one]
    assert client.register_calls == []

    media_verifier.fail_copy_ids.clear()
    prompts.available.update({copy_one, copy_two})
    prompts.confirmed.update({copy_one, copy_two})
    second = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "verifying burned media for 20260420T040001Z-1" in second.stderr
    assert "burning copy 20260420T040001Z-1" not in second.stderr
    assert burner.calls == [copy_one, copy_two]
    assert media_verifier.calls == [copy_one, copy_one, copy_two]
    assert client.register_calls == [copy_one, copy_two]
    assert client.copy_states[copy_one]["state"] == "verified"
    assert client.copy_states[copy_two]["verification_state"] == "verified"


def test_arc_disc_burn_redownloads_invalid_staged_iso(monkeypatch, tmp_path: Path) -> None:
    copy_one = "20260420T040001Z-1"
    copy_two = "20260420T040001Z-2"

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

        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
            assert device == "/dev/fake-sr0"

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

    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert first.exit_code == 1
    assert "fixture burn failed for 20260420T040001Z-2" in first.stderr
    assert client.download_calls == 1

    staged_iso = tmp_path / "20260420T040001Z" / "20260420T040001Z.iso"
    staged_iso.write_bytes(b"corrupted-iso\n")
    burner.fail_copy_ids.clear()
    prompts.confirmed.add(copy_two)
    prompts.available.add(copy_two)

    second = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "staged ISO is invalid" in second.stderr
    assert "re-downloading" in second.stderr
    assert client.download_calls == 2
    assert client.copy_states[copy_two]["state"] == "verified"


def test_arc_disc_burn_reburns_when_unlabeled_disc_is_unavailable_on_resume(
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

        def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
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

    monkeypatch.setattr(arc_disc_main, "ApiClient", lambda: client)
    monkeypatch.setattr(arc_disc_main, "build_iso_verifier", lambda: FakeIsoVerifier())
    monkeypatch.setattr(arc_disc_main, "build_disc_burner", lambda: burner)
    monkeypatch.setattr(arc_disc_main, "build_burned_media_verifier", lambda: FakeMediaVerifier())
    monkeypatch.setattr(arc_disc_main, "build_burn_prompts", lambda: prompts)

    first = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert first.exit_code == 1
    assert burner.calls == [copy_one]

    prompts.confirmed.update({copy_one, copy_two})
    prompts.available.add(copy_two)
    second = runner.invoke(
        arc_disc_main.app,
        ["burn", "--device", "/dev/fake-sr0", "--staging-dir", str(tmp_path)],
    )

    assert second.exit_code == 0
    assert "unlabeled disc for 20260420T040001Z-1 is unavailable; restarting burn" in second.stderr
    assert burner.calls == [copy_one, copy_one, copy_two]
    assert client.register_calls == [copy_one, copy_two]
