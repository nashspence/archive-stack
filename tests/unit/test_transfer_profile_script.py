from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "transfer_profile.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("transfer_profile", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_transfer_log_summary_selects_scenario_and_omits_identity() -> None:
    module = load_script()
    text = (
        "ignored line\n"
        "transfer operation=pack_write_segment identity_sha256=secret-digest "
        "plaintext_bytes=1048576 stored_bytes=1048600 queue_seconds=0.1 "
        "source_seconds=0.2 integrity_seconds=0.3 crypto_seconds=0.4 "
        "processing_seconds=0.5 remote_seconds=0.6 checkpoint_seconds=0.7 "
        "downstream_seconds=0.8 elapsed_seconds=3.6 bottleneck=downstream\n"
        "transfer operation=pack_retrieval_range identity_sha256=other "
        "plaintext_bytes=1 stored_bytes=2 remote_seconds=10 bottleneck=remote\n"
    )

    summary = module.summarize_transfer_log(
        text,
        expected_operations=module.SCENARIO_OPERATIONS["riverhog-ingress"],
    )

    assert summary.records == 1
    assert summary.operations == {"pack_write_segment": 1}
    assert summary.bottlenecks == {"downstream": 1}
    assert summary.plaintext_bytes == 1048576
    assert summary.stored_bytes == 1048600
    assert summary.phase_seconds == {
        "checkpoint": 0.7,
        "crypto": 0.4,
        "downstream": 0.8,
        "integrity": 0.3,
        "processing": 0.5,
        "queue": 0.1,
        "remote": 0.6,
        "source": 0.2,
    }
    assert "secret-digest" not in repr(summary)


def test_transfer_profile_runs_without_echoing_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    log = tmp_path / "transfer.log"
    log.write_text(
        "transfer operation=raw_write_segment identity_sha256=private "
        "plaintext_bytes=2097152 stored_bytes=2097200 queue_seconds=0 "
        "source_seconds=0.1 crypto_seconds=0.2 remote_seconds=0.3 "
        "checkpoint_seconds=0.1 downstream_seconds=0 elapsed_seconds=0.7 "
        "bottleneck=remote\n",
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def run(
        command: list[str],
        *,
        check: bool,
        stdout: int,
        stderr: int,
    ) -> subprocess.CompletedProcess[str]:
        assert not check
        assert stdout == subprocess.DEVNULL
        assert stderr == subprocess.DEVNULL
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", run)
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(ticks))

    assert (
        module.main(
            [
                "--scenario",
                "riverhog-ingress",
                "--workload",
                "large-file",
                "--payload-bytes",
                str(200 * module.MIB),
                "--baseline-mib-per-second",
                "125",
                "--transfer-log",
                str(log),
                "--",
                "riverhog",
                "upload",
                "/private/input",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert commands == [["riverhog", "upload", "/private/input"]]
    assert result["mib_per_second"] == 100.0
    assert result["utilization"] == 0.8
    assert result["target_utilization"] == 0.9
    assert result["items_per_second"] == 0.5
    assert result["seconds_per_item"] == 2.0
    assert result["transfer_log"]["operations"] == {"raw_write_segment": 1}
    assert "/private/input" not in json.dumps(result)


def test_network_profile_requires_a_raw_baseline() -> None:
    module = load_script()

    with pytest.raises(SystemExit):
        module.main(
            [
                "--scenario",
                "archive-replication",
                "--workload",
                "resume",
                "--payload-bytes",
                "1",
                "--",
                "true",
            ]
        )


def test_reference_recovery_profile_does_not_require_network_baseline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
    )
    ticks = iter((1.0, 2.0))
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(ticks))

    assert (
        module.main(
            [
                "--scenario",
                "reference-recovery",
                "--workload",
                "many-small-files",
                "--payload-bytes",
                str(module.MIB),
                "--items",
                "20",
                "--",
                "riverhog-recover",
                "archive",
                "output",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["baseline_mib_per_second"] is None
    assert result["target_utilization"] is None
    assert result["items"] == 20
    assert result["items_per_second"] == 20.0
    assert result["seconds_per_item"] == 0.05
    assert os.access(SCRIPT, os.X_OK)
