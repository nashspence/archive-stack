from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_uses_internal_service_urls():
    repo_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repo_root / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    api_env = services["api"]["environment"]
    ui_env = services["ui"]["environment"]
    tusd_service = services["tusd"]
    tusd_command = tusd_service["command"]

    assert api_env["TUSD_BASE_URL"] == "http://tusd:1080/files"
    assert ui_env["RIVERHOG_API_BASE_URL"] == "http://api:8080"
    assert tusd_service["user"] == "0:0"
    assert tusd_command[0].startswith("-")
    assert "-port=1080" in tusd_command
    assert "-upload-dir=/var/lib/archive/tusd" in tusd_command
