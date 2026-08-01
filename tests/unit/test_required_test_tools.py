from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.parametrize(
    ("command", "arguments", "expected_version"),
    [
        ("age", ("--version",), "v1.3.1"),
        ("age-plugin-batchpass", ("--version",), "v1.3.1"),
        ("exiftool", ("-ver",), "13.59"),
        ("minisign", ("-v",), "minisign 0.12"),
    ],
)
def test_required_native_test_tool_is_available(
    command: str,
    arguments: tuple[str, ...],
    expected_version: str,
) -> None:
    executable = shutil.which(command)
    assert executable is not None, f"{command} is missing from the locked mise test toolchain"

    completed = subprocess.run(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert expected_version in completed.stdout + completed.stderr
