from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, unquote, urlsplit

from riverhog_api.urls import public_tusd_upload_url


def _secure_link_token(*, expires: str, path: str, secret: str) -> str:
    digest = hashlib.md5(f"{expires}{unquote(path)} {secret}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def test_public_tusd_upload_url_rewrites_base_without_signing(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_BASE_URL", "http://riverhog-tusd:1080/files")
    monkeypatch.setenv("RIVERHOG_TUSD_PUBLIC_BASE_URL", "https://riverhog.test/files")
    monkeypatch.delenv("RIVERHOG_TUSD_PUBLIC_SIGNING_SECRET", raising=False)

    url = public_tusd_upload_url(
        "http://riverhog-tusd:1080/files/.riverhog%2Fuploads%2Fby-target%2Fabc"
    )

    assert url == "https://riverhog.test/files/.riverhog%2Fuploads%2Fby-target%2Fabc"


def test_public_tusd_upload_url_signs_nginx_normalized_path(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_BASE_URL", "http://riverhog-tusd:1080/files")
    monkeypatch.setenv("RIVERHOG_TUSD_PUBLIC_BASE_URL", "https://riverhog.test/files")
    monkeypatch.setenv("RIVERHOG_TUSD_PUBLIC_SIGNING_SECRET", "fixture-secret")

    url = public_tusd_upload_url(
        "http://riverhog-tusd:1080/files/.riverhog%2Fuploads%2Fby-target%2Fabc",
        expires_at="2026-06-13T18:19:20Z",
    )

    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    expires = str(1781374760)
    assert parsed.scheme == "https"
    assert parsed.netloc == "riverhog.test"
    assert parsed.path == "/files/.riverhog%2Fuploads%2Fby-target%2Fabc"
    assert query == {
        "expires": [expires],
        "md5": [
            _secure_link_token(
                expires=expires,
                path=parsed.path,
                secret="fixture-secret",
            )
        ],
    }


def test_public_tusd_upload_url_omits_signature_without_expiry(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_BASE_URL", "http://riverhog-tusd:1080/files")
    monkeypatch.setenv("RIVERHOG_TUSD_PUBLIC_BASE_URL", "https://riverhog.test/files")
    monkeypatch.setenv("RIVERHOG_TUSD_PUBLIC_SIGNING_SECRET", "fixture-secret")

    url = public_tusd_upload_url(
        "http://riverhog-tusd:1080/files/.riverhog%2Fuploads%2Fby-target%2Fabc"
    )

    assert url == "https://riverhog.test/files/.riverhog%2Fuploads%2Fby-target%2Fabc"
