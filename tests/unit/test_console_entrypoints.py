from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from jeb_cli.main import build_parser as build_jeb_parser
from munchy_api import app as munchy_api_app
from munchy_av1_nvenc import main as munchy_av1_nvenc_app
from munchy_cli.main import app as munchy_app
from riverhog_cli.main import app as riverhog_app
from typer.main import get_command

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

LIFECYCLE_EVENT_LIST_COMMANDS = (
    ("riverhog", "event", "list", "--help"),
    ("munchy", "event", "list", "--help"),
    ("jeb", "event", "list", "--help"),
)

PAGED_LIST_COMMANDS = (
    ("riverhog", "collection", "list", "--help"),
    ("riverhog", "collection", "upload", "list", "--help"),
    ("riverhog", "collection", "provenance", "list", "--help"),
    ("riverhog", "find", "--help"),
    ("riverhog", "tag", "list", "--help"),
    ("riverhog", "archive", "copy", "list", "--help"),
    ("riverhog", "archive", "store", "list", "--help"),
    ("riverhog", "retrieval", "cache", "list", "--help"),
    ("riverhog", "app", "list", "--help"),
    ("riverhog", "app", "key", "list", "--help"),
    ("riverhog", "app", "key", "access", "list", "--help"),
    ("riverhog", "app", "key", "quota", "list", "--help"),
    ("riverhog", "local", "list", "--help"),
    ("munchy", "app", "list", "--help"),
    ("munchy", "app", "key", "list", "--help"),
    ("munchy", "template", "list", "--help"),
    ("munchy", "job", "list", "--help"),
    ("munchy", "job", "diagnostic", "list", "--help"),
    ("jeb", "operation", "list", "--help"),
    ("jeb", "source", "list", "--help"),
    ("jeb", "attempt", "list", "--help"),
)

BOUNDED_LIST_COMMANDS = (("riverhog", "collection", "tag", "list", "--help"),)


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
    LIFECYCLE_EVENT_LIST_COMMANDS,
)
def test_lifecycle_event_cli_help_uses_the_shared_contract(command: tuple[str, ...]) -> None:
    completed = _run_help(command)

    assert completed.returncode == 0, completed.stderr
    for option in ("--after", "--limit", "--json"):
        assert option in completed.stdout


@pytest.mark.parametrize(
    "command",
    PAGED_LIST_COMMANDS,
)
def test_paged_list_cli_help_uses_the_shared_contract(command: tuple[str, ...]) -> None:
    completed = _run_help(command)

    assert completed.returncode == 0, completed.stderr
    for option in ("--page", "--per-page", "--sort", "--order", "--query", "--all", "--json"):
        assert option in completed.stdout


@pytest.mark.parametrize("command", BOUNDED_LIST_COMMANDS)
def test_bounded_list_cli_help_uses_the_shared_output_contract(command: tuple[str, ...]) -> None:
    completed = _run_help(command)

    assert completed.returncode == 0, completed.stderr
    for option in ("--ids", "--json"):
        assert option in completed.stdout


def test_retrieval_cache_list_emits_actionable_composite_selectors() -> None:
    completed = _run_help(("riverhog", "retrieval", "cache", "list", "--help"))

    assert completed.returncode == 0, completed.stderr
    assert "--selectors" in completed.stdout
    for option in (
        "--tag",
        "--collection",
        "--source-store",
        "--state",
        "--protection",
        "--expires-before",
        "--expires-after",
    ):
        assert option in completed.stdout


def _typer_list_commands(command: Any, prefix: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    for name, child in getattr(command, "commands", {}).items():
        path = (*prefix, str(name))
        if name == "list":
            yield path
        yield from _typer_list_commands(child, path)


def _argparse_list_commands(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...],
) -> Iterator[tuple[str, ...]]:
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in action.choices.items():
            path = (*prefix, name)
            if name == "list":
                yield path
            yield from _argparse_list_commands(child, path)


def test_every_official_list_command_has_one_declared_convention() -> None:
    discovered = {
        *_typer_list_commands(get_command(riverhog_app), ("riverhog",)),
        *_typer_list_commands(get_command(munchy_app), ("munchy",)),
        *_argparse_list_commands(build_jeb_parser(), ("jeb",)),
    }
    classified = {
        command[:-1]
        for command in (
            *LIFECYCLE_EVENT_LIST_COMMANDS,
            *PAGED_LIST_COMMANDS,
            *BOUNDED_LIST_COMMANDS,
        )
        if command[-2] == "list"
    }

    assert discovered == classified
