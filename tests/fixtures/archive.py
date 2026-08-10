from __future__ import annotations

import base64
import json


def age_state_json(plaintext_bytes: int) -> str:
    if plaintext_bytes < 0:
        raise ValueError("plaintext bytes must be non-negative")
    payload = {
        "format": "age-v1-scrypt-resumable",
        "header_b64": _b64(b"\xfb\xfffixture-age-header"),
        "payload_nonce_b64": _b64(b"\xfb\xfffixture-nonce-"),
        "plaintext_size": plaintext_bytes,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii").rstrip("=")
