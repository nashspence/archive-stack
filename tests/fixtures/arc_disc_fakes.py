from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def _fixture() -> dict[str, Any]:
    path = os.environ["ARC_DISC_FIXTURE_PATH"]
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        if copy_id in burn.get("fail_copy_ids", []):
            raise RuntimeError(f"fixture burn failed for {copy_id} on {device}")
        if not iso_path.is_file():
            raise RuntimeError(f"fixture burn source is missing for {copy_id}: {iso_path}")


class FixtureBurnedMediaVerifier:
    def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        if copy_id in burn.get("verify_fail_copy_ids", []):
            raise RuntimeError(
                f"fixture burned-media verification failed for {copy_id} on {device}"
            )
        if not iso_path.is_file():
            raise RuntimeError(f"fixture verification source is missing for {copy_id}: {iso_path}")


class FixtureBurnPrompts:
    def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        if copy_id in burn.get("blank_media_blocked_copy_ids", []):
            raise RuntimeError(f"fixture blank media unavailable for {copy_id} on {device}")

    def confirm_label(self, copy_id: str, *, label_text: str) -> None:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        confirmed = set(burn.get("confirmed_copy_ids", []))
        if copy_id not in confirmed:
            raise RuntimeError(f"label confirmation required for {copy_id}")
        expected = burn.get("label_text_by_copy_id", {})
        if expected and expected.get(copy_id) not in {None, label_text}:
            raise RuntimeError(f"fixture label text mismatch for {copy_id}")

    def prompt_location(self, copy_id: str) -> str:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        location_by_copy_id = burn.get("location_by_copy_id", {})
        try:
            return str(location_by_copy_id[copy_id])
        except KeyError as exc:
            raise RuntimeError(f"storage location required for {copy_id}") from exc

    def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
        fixture = _fixture()
        burn = fixture.get("burn", {})
        available = set(burn.get("available_copy_ids", []))
        return copy_id in available
