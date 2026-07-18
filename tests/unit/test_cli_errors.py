from __future__ import annotations

import httpx
import pytest

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

    exit_code = riverhog_main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
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
        riverhog_main.sys,
        "argv",
        ["riverhog", "collection", "show", "2025/20250102T030405Z__docs", "--json"],
    )

    exit_code = riverhog_main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == (
        '{"error":{"code":"not_found","message":"collection not found: docs"}}\n'
    )
    assert captured.err == ""


def test_main_prints_http_status_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = httpx.Request("POST", "https://riverhog.test/v1/retrieval-jobs/job/ack")
    response = httpx.Response(502, request=request)

    def fail_app() -> None:
        response.raise_for_status()

    monkeypatch.setattr(riverhog_main, "app", fail_app)
    monkeypatch.setattr(riverhog_main.sys, "argv", ["riverhog", "local", "sync"])

    exit_code = riverhog_main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "riverhog: service returned HTTP 502\n"
    assert "Traceback" not in captured.err


def test_main_prints_json_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = httpx.Request("GET", "https://riverhog.test/v1/retrieval-jobs/job")
    response = httpx.Response(503, request=request)

    def fail_app() -> None:
        response.raise_for_status()

    monkeypatch.setattr(riverhog_main, "app", fail_app)
    monkeypatch.setattr(riverhog_main.sys, "argv", ["riverhog", "local", "sync", "--json"])

    exit_code = riverhog_main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == (
        '{"error":{"code":"service_unavailable","message":"service returned HTTP 503"}}\n'
    )
    assert captured.err == ""
