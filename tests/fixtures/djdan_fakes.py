from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _fixture() -> dict[str, Any]:
    path = os.environ["DJDAN_FIXTURE_PATH"]
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_fixture(payload: dict[str, Any]) -> None:
    path = os.environ["DJDAN_FIXTURE_PATH"]
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class FixtureOpticalReader:
    def read_iter(self, disc_path: str, *, device: str) -> Iterator[bytes]:
        fixture = _fixture()
        reader = fixture["reader"]
        if disc_path in reader["fail_disc_paths"]:
            raise RuntimeError(f"fixture optical read failed for {disc_path} on {device}")
        try:
            encoded = reader["payload_by_disc_path"][disc_path]
        except KeyError as exc:
            raise RuntimeError(f"missing recovery fixture for {disc_path}") from exc
        yield base64.b64decode(encoded)


class FixtureIsoVerifier:
    def verify(self, iso_path: Path) -> None:
        if not iso_path.is_file():
            raise RuntimeError(f"fixture staged ISO is missing: {iso_path}")
        if iso_path.stat().st_size <= 0:
            raise RuntimeError(f"fixture staged ISO is empty: {iso_path}")


class FixtureDiscBurner:
    def burn(self, iso_path: Path, *, device: str, disc_id: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        if disc_id in burn.get("fail_disc_ids", []):
            raise RuntimeError(f"fixture burn failed for {disc_id} on {device}")
        if not iso_path.is_file():
            raise RuntimeError(f"fixture burn source is missing for {disc_id}: {iso_path}")


class FixtureBurnedMediaVerifier:
    def verify(self, iso_path: Path, *, device: str, disc_id: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        fail_once_disc_ids = set(burn.get("verify_fail_once_disc_ids", []))
        if disc_id in fail_once_disc_ids:
            fail_once_disc_ids.remove(disc_id)
            burn["verify_fail_once_disc_ids"] = sorted(fail_once_disc_ids)
            _write_fixture(fixture)
            raise RuntimeError(
                f"fixture burned-media verification failed for {disc_id} on {device}"
            )
        if disc_id in burn.get("verify_fail_disc_ids", []):
            raise RuntimeError(
                f"fixture burned-media verification failed for {disc_id} on {device}"
            )
        if not iso_path.is_file():
            raise RuntimeError(f"fixture verification source is missing for {disc_id}: {iso_path}")


class FixtureBurnPrompts:
    def wait_for_blank_disc(
        self, disc_id: str, *, device: str, target_bytes: int | None = None
    ) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        if disc_id in burn.get("blank_media_blocked_disc_ids", []):
            raise RuntimeError(f"fixture blank media unavailable for {disc_id} on {device}")

    def confirm_label(self, disc_id: str, *, label_text: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        confirmed = set(burn.get("confirmed_disc_ids", []))
        if disc_id not in confirmed:
            raise RuntimeError(f"label confirmation required for {disc_id}")
        expected = burn.get("label_text_by_disc_id", {})
        if expected and expected.get(disc_id) not in {None, label_text}:
            raise RuntimeError(f"fixture label text mismatch for {disc_id}")

    def prompt_location(self, disc_id: str) -> str:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        location_by_disc_id = burn.get("location_by_disc_id", {})
        try:
            return str(location_by_disc_id[disc_id])
        except KeyError as exc:
            raise RuntimeError(f"storage location required for {disc_id}") from exc

    def confirm_unlabeled_disc_available(self, disc_id: str) -> bool:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        available = set(burn.get("available_disc_ids", []))
        return disc_id in available
