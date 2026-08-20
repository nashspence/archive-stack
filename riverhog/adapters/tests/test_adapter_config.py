from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from riverhog_adapters.config import load_config

REPO_ROOT = Path(__file__).parents[3]


def test_adapter_secrets_accept_exactly_one_direct_or_file_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "adapters.json"
    config_path.write_text(
        json.dumps(
            {
                "host_id": "urn:uuid:00000000-0000-4000-8000-000000000001",
                "riverhog_base_url": "https://riverhog.invalid",
                "sources": [
                    {
                        "id": "drop",
                        "adapter": "watched-drop",
                        "root": str(tmp_path / "drop"),
                        "ingest_source": "watched:drop",
                        "tags": ["drop"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    riverhog_token = tmp_path / "riverhog.token"
    adapter_token = tmp_path / "adapter.token"
    riverhog_token.write_text("riverhog-file-token\n", encoding="utf-8")
    adapter_token.write_text("adapter-file-token\n", encoding="utf-8")
    monkeypatch.setenv("RIVERHOG_TOKEN_FILE", str(riverhog_token))
    monkeypatch.setenv("RIVERHOG_ADAPTERS_API_TOKEN_FILE", str(adapter_token))

    config = load_config(config_path)

    assert config.riverhog_token == "riverhog-file-token"
    assert config.api_token == "adapter-file-token"
    monkeypatch.setenv("RIVERHOG_TOKEN", "direct")
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_config(config_path)


def test_reference_compose_keeps_adapters_content_opaque_bounded_and_unprivileged() -> None:
    path = REPO_ROOT / "riverhog/adapters/compose.yaml"
    compose = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"adapter", "ftp", "intake-init", "tus", "tusd"}
    assert services["adapter"]["read_only"] is True
    assert services["adapter"]["user"] == "65532:65532"
    assert services["adapter"]["group_add"] == ["${RIVERHOG_ADAPTERS_SECRET_FILE_GID:-65532}"]
    assert services["intake-init"]["environment"] == {
        "INTAKE_GID": "${RIVERHOG_ADAPTERS_INTAKE_GID:-65532}"
    }
    assert '-g "$${INTAKE_GID}" -m 2770' in services["intake-init"]["command"][-1]
    assert services["tusd"]["user"] == "1000:65532"
    assert services["tus"]["read_only"] is True
    assert services["tus"]["user"] == "101:101"
    assert services["ftp"]["profiles"] == ["ftp"]
    assert "FTP_USER_PASS" not in services["ftp"]["environment"]
    assert services["ftp"]["secrets"] == ["ftp_password"]
    assert "/run/secrets/ftp_password" in services["ftp"]["command"][-1]
    assert services["tusd"]["profiles"] == ["tus"]
    assert services["adapter"]["networks"] == ["default", "riverhog-control"]
    assert compose["networks"]["riverhog-control"] == {
        "external": True,
        "name": "${RIVERHOG_CONTROL_NETWORK:-riverhog_default}",
    }
    config_mount = next(
        item
        for item in services["adapter"]["volumes"]
        if item["target"] == "/etc/riverhog/adapters.json"
    )
    assert config_mount["source"] == (
        "${RIVERHOG_ADAPTERS_CONFIG_HOST_PATH:-./config/adapters.example.json}"
    )
    text = path.read_text(encoding="utf-8")
    assert "proxy_request_buffering off" not in text
    proxy = (REPO_ROOT / "riverhog/adapters/config/tus-nginx.conf").read_text(encoding="utf-8")
    assert "proxy_request_buffering off;" in proxy
    assert "auth_request /_riverhog_tus_auth;" in proxy
    assert "archive" not in {key.casefold() for key in compose.get("volumes", {})}


def test_reference_adapter_configuration_is_current_and_secret_injected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    riverhog_token = tmp_path / "riverhog.token"
    adapter_token = tmp_path / "adapter.token"
    tus_password = tmp_path / "tus.token"
    riverhog_token.write_text("riverhog\n", encoding="utf-8")
    adapter_token.write_text("adapter\n", encoding="utf-8")
    tus_password.write_text("tus\n", encoding="utf-8")
    monkeypatch.setenv("RIVERHOG_TOKEN_FILE", str(riverhog_token))
    monkeypatch.setenv("RIVERHOG_ADAPTERS_API_TOKEN_FILE", str(adapter_token))
    monkeypatch.setenv("RIVERHOG_TUS_PASSWORD_FILE", str(tus_password))

    config = load_config(REPO_ROOT / "riverhog/adapters/config/adapters.example.json")

    assert config.host_id == "urn:uuid:00000000-0000-4000-8000-000000000001"
    assert config.riverhog_base_url == "http://app:8000"
    assert config.poll_seconds == 5
    assert [source.adapter for source in config.sources] == [
        "ftp",
        "tus",
        "watched-drop",
    ]
    ftp = config.source("ftp-intake")
    assert ftp.close_mode == "stable"
    assert ftp.provenance_omission_reason == (
        "The FTP producer cannot observe the source host filesystem."
    )
    tus = config.source("tus-intake")
    assert tus.close_mode == "explicit-flush"
    assert tus.credential_env == "RIVERHOG_TUS_PASSWORD"
    assert tus.credential() == "tus"
    assert all(source.max_bytes > 0 and source.max_files > 0 for source in config.sources)


def test_adapter_config_path_environment_is_connected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "adapters.json"
    config_path.write_text(
        json.dumps(
            {
                "host_id": "urn:uuid:00000000-0000-4000-8000-000000000001",
                "riverhog_base_url": "https://riverhog.invalid",
                "riverhog_token": "riverhog-token",
                "api_token": "adapter-token",
                "sources": [
                    {
                        "id": "drop",
                        "adapter": "watched-drop",
                        "root": str(tmp_path / "drop"),
                        "ingest_source": "watched:drop",
                        "tags": ["drop"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RIVERHOG_ADAPTERS_CONFIG", str(config_path))

    assert load_config().host_id == "urn:uuid:00000000-0000-4000-8000-000000000001"


def test_capture_configuration_requires_a_canonical_provenance_host_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "adapters.json"
    config_path.write_text(
        json.dumps(
            {
                "host_id": "human-label",
                "riverhog_base_url": "https://riverhog.invalid",
                "riverhog_token": "riverhog-token",
                "api_token": "adapter-token",
                "sources": [
                    {
                        "id": "drop",
                        "adapter": "watched-drop",
                        "root": str(tmp_path / "drop"),
                        "ingest_source": "watched:drop",
                        "tags": ["drop"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("RIVERHOG_TOKEN", raising=False)
    monkeypatch.delenv("RIVERHOG_ADAPTERS_API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="host_id must be a lowercase UUID URN"):
        load_config(config_path)
