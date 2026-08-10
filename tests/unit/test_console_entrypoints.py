from __future__ import annotations

import importlib.metadata
import os
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


def _run_help(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"COLUMNS": "240", "NO_COLOR": "1", "TERM": "dumb"})
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=10,
    )


def test_munchy_http_api_versions_match_their_installed_distributions() -> None:
    assert munchy_api_app.app.version == importlib.metadata.version("munchy-server")
    assert munchy_av1_nvenc_app.app.version == importlib.metadata.version("munchy-av1-nvenc-target")


@pytest.mark.parametrize("command", CONSOLE_DISTRIBUTIONS)
def test_published_console_entrypoint_help_is_side_effect_free(command: str) -> None:
    completed = _run_help((command, "--help"))

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


@pytest.mark.parametrize(
    "command",
    (
        ("riverhog", "event", "list", "--help"),
        ("munchy", "event", "list", "--help"),
        ("jeb", "event", "list", "--help"),
    ),
)
def test_lifecycle_event_cli_help_uses_the_shared_contract(command: tuple[str, ...]) -> None:
    completed = _run_help(command)

    assert completed.returncode == 0, completed.stderr
    for option in ("--after", "--limit", "--json"):
        assert option in completed.stdout


@pytest.mark.parametrize(
    "command",
    (
        ("riverhog", "collection", "list", "--help"),
        ("riverhog", "collection", "upload", "list", "--help"),
        ("riverhog", "find", "--help"),
        ("riverhog", "tag", "list", "--help"),
        ("riverhog", "archive", "copy", "list", "--help"),
        ("riverhog", "archive", "store", "list", "--help"),
        ("riverhog", "app", "list", "--help"),
        ("riverhog", "app", "key", "list", "--help"),
        ("riverhog", "app", "key", "access", "list", "--help"),
        ("riverhog", "app", "key", "quota", "list", "--help"),
        ("munchy", "app", "list", "--help"),
        ("munchy", "app", "key", "list", "--help"),
        ("munchy", "template", "list", "--help"),
        ("munchy", "job", "list", "--help"),
        ("munchy", "job", "diagnostic", "list", "--help"),
        ("jeb", "operation", "list", "--help"),
        ("jeb", "source", "list", "--help"),
        ("jeb", "attempt", "list", "--help"),
    ),
)
def test_paged_list_cli_help_uses_the_shared_contract(command: tuple[str, ...]) -> None:
    completed = _run_help(command)

    assert completed.returncode == 0, completed.stderr
    for option in ("--page", "--per-page", "--sort", "--order", "--query", "--all", "--json"):
        assert option in completed.stdout
