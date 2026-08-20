from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from riverhog_ftp_adapter.config import load_config

REPO_ROOT = Path(__file__).parents[3]


def _write_config(path: Path, root: Path, *, host_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "host_id": host_id,
                "riverhog_base_url": "https://riverhog.invalid",
                "sources": [
                    {
                        "id": "ftp",
                        "root": str(root),
                        "ingest_source": "ftp:fixture",
                        "tags": ["ftp"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_adapter_secrets_accept_exactly_one_direct_or_file_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "ftp-adapter.json"
    _write_config(
        config_path,
        tmp_path / "ftp",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
    )
    riverhog_token = tmp_path / "riverhog.token"
    adapter_token = tmp_path / "adapter.token"
    riverhog_token.write_text("riverhog-file-token\n", encoding="utf-8")
    adapter_token.write_text("adapter-file-token\n", encoding="utf-8")
    monkeypatch.setenv("RIVERHOG_TOKEN_FILE", str(riverhog_token))
    monkeypatch.setenv("RIVERHOG_FTP_ADAPTER_API_TOKEN_FILE", str(adapter_token))

    config = load_config(config_path)

    assert config.riverhog_token == "riverhog-file-token"
    assert config.api_token == "adapter-file-token"
    monkeypatch.setenv("RIVERHOG_TOKEN", "direct")
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_config(config_path)


def test_reference_compose_is_ftp_only_bounded_and_unprivileged() -> None:
    path = REPO_ROOT / "riverhog/ftp-adapter/compose.yaml"
    compose = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"ftp-adapter", "ftp-daemon", "intake-init"}
    assert services["ftp-adapter"]["read_only"] is True
    assert services["ftp-adapter"]["user"] == "65532:65532"
    assert services["ftp-adapter"]["group_add"] == [
        "${RIVERHOG_FTP_ADAPTER_SECRET_FILE_GID:-65532}"
    ]
    assert services["ftp-adapter"]["networks"] == ["default", "riverhog-control"]
    assert "FTP_USER_PASS" not in services["ftp-daemon"]["environment"]
    assert services["ftp-daemon"]["secrets"] == ["ftp_password"]
    assert "/run/secrets/ftp_password" in services["ftp-daemon"]["command"][-1]
    assert compose["networks"]["riverhog-control"] == {
        "external": True,
        "name": "${RIVERHOG_CONTROL_NETWORK:-riverhog_default}",
    }
    config_mount = next(
        item
        for item in services["ftp-adapter"]["volumes"]
        if item["target"] == "/etc/riverhog/ftp-adapter.json"
    )
    assert config_mount["source"] == (
        "${RIVERHOG_FTP_ADAPTER_CONFIG_HOST_PATH:-./config/ftp-adapter.example.json}"
    )
    assert "archive" not in {key.casefold() for key in compose.get("volumes", {})}


def test_reference_configuration_is_current_and_secret_injected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    riverhog_token = tmp_path / "riverhog.token"
    adapter_token = tmp_path / "adapter.token"
    riverhog_token.write_text("riverhog\n", encoding="utf-8")
    adapter_token.write_text("adapter\n", encoding="utf-8")
    monkeypatch.setenv("RIVERHOG_TOKEN_FILE", str(riverhog_token))
    monkeypatch.setenv("RIVERHOG_FTP_ADAPTER_API_TOKEN_FILE", str(adapter_token))

    config = load_config(
        REPO_ROOT / "riverhog/ftp-adapter/config/ftp-adapter.example.json"
    )

    assert config.host_id == "urn:uuid:00000000-0000-4000-8000-000000000001"
    assert config.riverhog_base_url == "http://app:8000"
    assert config.poll_seconds == 5
    assert [source.id for source in config.sources] == ["ftp-intake"]
    source = config.source("ftp-intake")
    assert source.ingest_source == "ftp:example-intake"
    assert source.close_mode == "stable"
    assert source.stable_seconds == 30
    assert source.provenance_omission_reason == (
        "The FTP producer cannot observe the source host filesystem."
    )
    assert source.max_bytes > 0 and source.max_files > 0


def test_adapter_config_path_environment_is_connected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "ftp-adapter.json"
    _write_config(
        config_path,
        tmp_path / "ftp",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setenv("RIVERHOG_FTP_ADAPTER_CONFIG", str(config_path))
    monkeypatch.setenv("RIVERHOG_TOKEN", "riverhog")
    monkeypatch.setenv("RIVERHOG_FTP_ADAPTER_API_TOKEN", "adapter")

    assert load_config().host_id == "urn:uuid:00000000-0000-4000-8000-000000000001"


def test_capture_configuration_requires_a_canonical_provenance_host_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "ftp-adapter.json"
    _write_config(config_path, tmp_path / "ftp", host_id="human-label")
    monkeypatch.setenv("RIVERHOG_TOKEN", "riverhog")
    monkeypatch.setenv("RIVERHOG_FTP_ADAPTER_API_TOKEN", "adapter")

    with pytest.raises(ValueError, match="host_id must be a lowercase UUID URN"):
        load_config(config_path)
