from __future__ import annotations

import httpx
import pytest
import typer

from riverhog_cli import main as riverhog_main
from riverhog_core.domain.errors import NotFound


def test_main_prints_transport_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_app() -> None:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(riverhog_main, "app", fail_app)
    monkeypatch.setattr(riverhog_main.sys, "argv", ["riverhog", "collection", "list"])

    with pytest.raises(typer.Exit) as exc_info:
        riverhog_main.main()

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 1
    assert captured.out == ""
    assert captured.err == "riverhog: transport error: connection refused\n"
    assert "Traceback" not in captured.err


def test_main_prints_json_error_for_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_app() -> None:
        raise NotFound("collection not found: docs")

    monkeypatch.setattr(riverhog_main, "app", fail_app)
    monkeypatch.setattr(
        riverhog_main.sys, "argv", ["riverhog", "collection", "show", "docs", "--json"]
    )

    with pytest.raises(typer.Exit) as exc_info:
        riverhog_main.main()

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == 1
    assert captured.out == (
        '{"error":{"code":"not_found","message":"collection not found: docs"}}\n'
    )
    assert captured.err == ""
