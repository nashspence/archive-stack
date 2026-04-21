from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any


def _fixture() -> dict[str, Any]:
    path = os.environ["ARC_DISC_FIXTURE_PATH"]
    return json.loads(Path(path).read_text(encoding="utf-8"))


class FixtureOpticalReader:
    def read(self, disc_path: str, *, device: str) -> bytes:
        fixture = _fixture()
        reader = fixture["reader"]
        if disc_path in reader["fail_disc_paths"]:
            raise RuntimeError(f"fixture optical read failed for {disc_path} on {device}")
        try:
            encoded = reader["encrypted_by_disc_path"][disc_path]
        except KeyError as exc:
            raise RuntimeError(f"missing encrypted fixture for {disc_path}") from exc
        return base64.b64decode(encoded)


class FixtureCrypto:
    def decrypt_entry(self, encrypted: bytes, enc: dict[str, Any]) -> bytes:
        fixture = _fixture()
        fixture_key = str(enc["fixture_key"])
        try:
            encoded = fixture["crypto"]["plaintext_by_fixture_key"][fixture_key]
        except KeyError as exc:
            raise RuntimeError(f"missing plaintext fixture for {fixture_key}") from exc
        return base64.b64decode(encoded)
