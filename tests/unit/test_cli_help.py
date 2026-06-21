from __future__ import annotations

import pytest
from typer.testing import CliRunner

from djdan.main import app as disc_app
from jeb.cli import main as jeb_main
from riverhog_cli.main import app as riverhog_app

runner = CliRunner()


def test_riverhog_help() -> None:
    result = runner.invoke(riverhog_app, ["--help"])
    assert result.exit_code == 0
    assert "Riverhog collection and hot-storage CLI." in result.stdout
    assert "Search collection file targets." in result.stdout
    assert "Collection catalog and upload operations." in result.stdout
    assert "Pinned hot-storage set operations." in result.stdout
    assert "collection" in result.stdout
    assert "hot" in result.stdout


def test_riverhog_command_help_has_summaries() -> None:
    collection = runner.invoke(riverhog_app, ["collection", "--help"])
    assert collection.exit_code == 0
    for summary in (
        "List collections with storage coverage summaries.",
        "Upload a local directory as a collection.",
        "Cancel an open collection upload session.",
        "Wait for collection finalization to finish.",
        "Show collection storage and recovery details.",
    ):
        assert summary in collection.stdout

    hot = runner.invoke(riverhog_app, ["hot", "--help"])
    assert hot.exit_code == 0
    for summary in (
        "Pin a target into hot storage.",
        "Release a hot-storage pin.",
        "List pinned hot-storage sets.",
        "Show hot-storage fetch progress.",
        "Cloud archive fetch operations for hot-storage fetches.",
    ):
        assert summary in hot.stdout

    cloud_fetch = runner.invoke(riverhog_app, ["hot", "cloud-fetch", "--help"])
    assert cloud_fetch.exit_code == 0
    for summary in (
        "Show cloud-fetch recovery for one hot fetch.",
        "Start or resume cloud archive recovery for one hot fetch.",
        "Cancel active cloud archive recovery for one hot fetch.",
    ):
        assert summary in cloud_fetch.stdout


def test_djdan_help() -> None:
    result = runner.invoke(disc_app, ["--help"])
    assert result.exit_code == 0
    assert "Riverhog optical media CLI." in result.stdout
    assert "Run the guided hot-storage fetch workflow." in result.stdout
    assert "Run the guided burn-backlog workflow." in result.stdout
    assert "Image planning and download operations." in result.stdout
    assert "Burned disc catalog operations." in result.stdout
    assert "fetch" in result.stdout
    assert "image" in result.stdout
    assert "disc" in result.stdout
    assert "recover" not in result.stdout


def test_djdan_command_help_has_summaries() -> None:
    image = runner.invoke(disc_app, ["image", "--help"])
    assert image.exit_code == 0
    for summary in (
        "List finalized images.",
        "Show finalized image details.",
        "List image planner candidates.",
        "Download a finalized ISO image.",
        "Image rebuild recovery operations.",
    ):
        assert summary in image.stdout

    rebuild = runner.invoke(disc_app, ["image", "rebuild", "--help"])
    assert rebuild.exit_code == 0
    for summary in (
        "List image rebuild sessions.",
        "Show an image rebuild session.",
        "Pause an active image rebuild session.",
        "Resume a paused image rebuild session.",
    ):
        assert summary in rebuild.stdout

    disc = runner.invoke(disc_app, ["disc", "--help"])
    assert disc.exit_code == 0
    for summary in (
        "List registered burned discs.",
        "Show burned disc details.",
        "Register a physical disc copy.",
        "Update a disc location label.",
        "Mark a disc as lost.",
        "Mark a disc as damaged.",
        "Mark a disc copy as verified.",
    ):
        assert summary in disc.stdout


def test_jeb_help_has_command_summaries(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        jeb_main(["--help"])

    assert exc_info.value.code == 0
    stdout = capsys.readouterr().out
    assert "Weekly collector and automated uploader." in stdout
    assert "run           run continuously and process eligible batches" in stdout
    assert "once          discover and process one scheduler pass" in stdout
    assert "check-config  validate configuration and initialize state" in stdout
