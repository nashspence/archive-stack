from __future__ import annotations

import pytest
from typer.testing import CliRunner

from djdan.main import app as disc_app
from gogurt.cli import app as gogurt_app
from jeb.cli import main as jeb_main
from jeb.service_cli import main as jeb_service_main
from munchy_cli.main import app as munchy_app
from riverhog_cli.main import app as riverhog_app

runner = CliRunner()


def test_riverhog_help() -> None:
    result = runner.invoke(riverhog_app, ["--help"])
    assert result.exit_code == 0
    assert "Riverhog collection and hot-storage CLI." in result.stdout
    assert "Search collection file targets." in result.stdout
    assert "Collection catalog and upload operations." in result.stdout
    assert "Hot-storage operations." in result.stdout
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
        "Evict compliant hot files by selector.",
        "Named fetch manifest operations.",
    ):
        assert summary in hot.stdout

    fetch = runner.invoke(riverhog_app, ["hot", "fetch", "--help"])
    assert fetch.exit_code == 0
    for summary in (
        "Create a named editable fetch.",
        "Add target selectors to an editable fetch.",
        "Remove target selectors from an editable fetch.",
        "List named fetches.",
        "Show fetch preflight and progress summary.",
        "List selected files for a fetch.",
        "Queue a fetch for djdan, or for cloud recovery with --cloud.",
        "Cancel an active fetch and return it to draft.",
    ):
        assert summary in fetch.stdout
    assert "cloud-fetch" not in fetch.stdout


def test_gogurt_help() -> None:
    result = runner.invoke(gogurt_app, ["--help"])
    assert result.exit_code == 0
    assert "Gogurt route and trigger utility." in result.stdout

    for summary in (
        "List configured Gogurt routes.",
        "Render executable trigger scripts from Gogurt routes.",
        "Write a Gogurt marker file to a mounted volume.",
    ):
        assert summary in result.stdout


def test_gogurt_command_help_has_summaries() -> None:
    for command, summary in (
        ("list", "List configured Gogurt routes."),
        ("render", "Render executable trigger scripts from Gogurt routes."),
        ("write", "Write a Gogurt marker file to a mounted volume."),
    ):
        command_help = runner.invoke(gogurt_app, [command, "--help"])
        assert command_help.exit_code == 0
        assert summary in command_help.stdout


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
    ):
        assert summary in image.stdout
    assert "rebuild" not in image.stdout

    disc = runner.invoke(disc_app, ["disc", "--help"])
    assert disc.exit_code == 0
    for summary in (
        "List registered burned discs.",
        "Show burned disc details.",
        "Update a disc location label.",
        "Disc rebuild operations.",
    ):
        assert summary in disc.stdout

    rebuild = runner.invoke(disc_app, ["disc", "rebuild", "--help"])
    assert rebuild.exit_code == 0
    for summary in (
        "Disc rebuild operations.",
        "Start rebuild work for a lost or damaged disc.",
        "List disc rebuild sessions.",
        "Show a disc rebuild session.",
        "Pause an active disc rebuild session.",
        "Resume a paused disc rebuild session.",
    ):
        assert summary in rebuild.stdout

    rebuild_start = runner.invoke(disc_app, ["disc", "rebuild", "start", "--help"])
    assert rebuild_start.exit_code == 0
    for summary in (
        "Start rebuild work for a lost or damaged disc.",
        "copy_id",
        "--reason",
        "lost or damaged",
    ):
        assert summary in rebuild_start.stdout


def test_jeb_help_has_command_summaries(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        jeb_main(["--help"])

    assert exc_info.value.code == 0
    stdout = capsys.readouterr().out
    assert "Remote Jeb operator CLI." in stdout
    assert "status        show read-only collector status" in stdout
    assert "batches       list batch attempts" in stdout
    assert "once          request one scheduler pass" in stdout
    assert "archive-now   archive one account immediately" in stdout
    assert "check-config  validate deployed Jeb configuration" in stdout


def test_jeb_service_help_has_command_summaries(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        jeb_service_main(["--help"])

    assert exc_info.value.code == 0
    stdout = capsys.readouterr().out
    assert "Service-local Jeb collector and uploader." in stdout
    assert "run           run continuously and process eligible batches" in stdout
    assert "archive-now   archive one account immediately" in stdout


def test_munchy_job_help_has_resume_command() -> None:
    result = runner.invoke(munchy_app, ["job", "--help"])

    assert result.exit_code == 0
    assert "Resume a failed or cancelled runner job after repair." in result.stdout
