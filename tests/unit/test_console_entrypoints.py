from __future__ import annotations

import importlib.metadata
import subprocess

import pytest
from munchy_api import app as munchy_api_app
from munchy_av1_nvenc import main as munchy_av1_nvenc_app

CONSOLE_DISTRIBUTIONS = {
    "riverhog": "riverhog-client",
    "riverhog-api": "riverhog-server",
    "riverhog-recover": "riverhog-recover",
    "munchy": "munchy-client",
    "munchy-server": "munchy-server",
    "munchy-av1-nvenc": "munchy-av1-nvenc-target",
    "jeb": "jeb-client",
    "jeb-service": "jeb-server",
    "gogurt": "gogurt",
    "mango-fish": "mango-fish",
}


def test_munchy_http_api_versions_match_their_installed_distributions() -> None:
    assert munchy_api_app.app.version == importlib.metadata.version("munchy-server")
    assert munchy_av1_nvenc_app.app.version == importlib.metadata.version("munchy-av1-nvenc-target")


@pytest.mark.parametrize("command", CONSOLE_DISTRIBUTIONS)
def test_published_console_entrypoint_help_is_side_effect_free(command: str) -> None:
    completed = subprocess.run(
        [command, "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout.casefold()


@pytest.mark.parametrize("command,distribution", CONSOLE_DISTRIBUTIONS.items())
def test_published_console_entrypoint_reports_installed_version(
    command: str,
    distribution: str,
) -> None:
    completed = subprocess.run(
        [command, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == importlib.metadata.version(distribution)
