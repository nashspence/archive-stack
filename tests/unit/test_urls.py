from __future__ import annotations

from riverhog_api.urls import public_tusd_upload_url


def test_public_tusd_upload_url_rewrites_base_and_preserves_query(monkeypatch) -> None:
    monkeypatch.setenv("RIVERHOG_TUSD_BASE_URL", "http://riverhog-tusd:1080/files")
    monkeypatch.setenv("RIVERHOG_TUSD_PUBLIC_BASE_URL", "https://riverhog.test/files")

    url = public_tusd_upload_url(
        "http://riverhog-tusd:1080/files/.riverhog%2Fuploads%2Fby-target%2Fabc?part=1"
    )

    assert url == "https://riverhog.test/files/.riverhog%2Fuploads%2Fby-target%2Fabc?part=1"
