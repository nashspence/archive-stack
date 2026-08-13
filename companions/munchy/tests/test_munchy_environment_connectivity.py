from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from munchy_api import app as munchy_app
from munchy_av1_nvenc import main as av1
from munchy_core.adapters import external, gpu, riverhog


def test_av1_target_nondefault_environment_reaches_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    settings = {
        "MUNCHY_LOG_LEVEL": "ERROR",
        "MUNCHY_HTTPX_LOG_LEVEL": "CRITICAL",
        "MUNCHY_DATA_DIR": str(tmp_path / "target-data"),
        "MUNCHY_MAX_PARALLEL_ENCODES": "7",
        "MUNCHY_FFMPEG_TIMEOUT_SECONDS": "123.5",
        "MUNCHY_VIDEO_DECODE_MODE": "software",
        "MUNCHY_VIDEO_SCALE_MODE": "cuda",
        "MUNCHY_AV1_CQ": "19",
        "MUNCHY_AV1_PRESET": "p5",
        "MUNCHY_AV1_TUNE": "hq",
        "MUNCHY_AV1_LOOKAHEAD_LEVEL": "2",
        "MUNCHY_AV1_SPLIT_ENCODE_MODE": "auto",
        "MUNCHY_AV1_PIX_FMT": "yuv420p",
        "MUNCHY_AUDIO_BITRATE": "192k",
        "MUNCHY_QCUT_TARGET_SECONDS": "240",
        "MUNCHY_QCUT_MIN_SECONDS": "4",
        "MUNCHY_QCUT_MAX_SECONDS": "12",
    }
    with monkeypatch.context() as environment:
        for name, value in settings.items():
            environment.setenv(name, value)
        runtime = importlib.reload(av1)

        assert runtime.LOGGING["root"]["level"] == "ERROR"
        assert runtime.LOGGING["loggers"]["httpx"]["level"] == "CRITICAL"
        assert runtime.DATA_DIR == (tmp_path / "target-data").resolve()
        assert runtime.MAX_PARALLEL_ENCODES == 7
        assert runtime.FFMPEG_TIMEOUT_SECONDS == 123.5
        assert runtime.VIDEO_DECODE_MODE == "software"
        assert runtime.VIDEO_SCALE_MODE == "cuda"
        assert runtime.ARCHIVE_CQ == "19"
        assert runtime.ARCHIVE_PRESET == "p5"
        assert runtime.ARCHIVE_TUNE == "hq"
        assert runtime.ARCHIVE_LOOKAHEAD_LEVEL == "2"
        assert runtime.ARCHIVE_SPLIT_ENCODE_MODE == "auto"
        assert runtime.ARCHIVE_PIX_FMT == "yuv420p"
        assert runtime.ARCHIVE_AUDIO_BITRATE == "192k"
        assert runtime.QCUT_TARGET_SECONDS == 240
        assert runtime.QCUT_MIN_SECONDS == 4
        assert runtime.QCUT_MAX_SECONDS == 12
    importlib.reload(av1)


def test_munchy_adapter_nondefault_environment_reaches_runtime(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = {
        "MUNCHY_HANDOFF_ATTEMPTS": "9",
        "MUNCHY_EXTERNAL_HANDOFF_ENABLED": "true",
        "MUNCHY_COMMAND_HANDOFF_COMMAND": "handoff-command --flag",
        "MUNCHY_RCLONE_HANDOFF_COMMAND": "custom-rclone",
        "MUNCHY_RIVERHOG_HANDOFF_ENABLED": "true",
        "MUNCHY_RIVERHOG_FINALIZE_POLL_SECONDS": "7.5",
        "MUNCHY_UPSTREAM_EVENT_POLL_SECONDS": "8.5",
        "MUNCHY_GPU_MANAGER_URL": "https://gpu-manager.example.test/",
        "MUNCHY_GPU_TARGET_URL": "https://gpu-target.example.test/",
    }
    with monkeypatch.context() as environment:
        for name, value in settings.items():
            environment.setenv(name, value)
        external_runtime = importlib.reload(external)
        riverhog_runtime = importlib.reload(riverhog)
        gpu_runtime = gpu.HttpGpuPlatform()

        assert external_runtime.HANDOFF_ATTEMPTS == 9
        assert external_runtime.EXTERNAL_HANDOFF_ENABLED is True
        assert external_runtime.COMMAND_HANDOFF_COMMAND == "handoff-command --flag"
        assert external_runtime.RCLONE_HANDOFF_COMMAND == "custom-rclone"
        assert riverhog_runtime.RIVERHOG_HANDOFF_ENABLED is True
        assert riverhog_runtime.RIVERHOG_FINALIZE_POLL_SECONDS == 7.5
        assert riverhog_runtime.UPSTREAM_EVENT_POLL_SECONDS == 8.5
        assert gpu_runtime.manager_url == "https://gpu-manager.example.test"
        assert gpu_runtime.target_url == "https://gpu-target.example.test"
    importlib.reload(external)
    importlib.reload(riverhog)


def test_munchy_server_and_target_bind_environment_reaches_uvicorn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, dict[str, Any]]] = []

    def run(application: str, **kwargs: Any) -> None:
        calls.append((application, kwargs))

    monkeypatch.setenv("MUNCHY_HOST", "127.0.0.42")
    monkeypatch.setenv("MUNCHY_PORT", "9123")
    monkeypatch.setenv("MUNCHY_UVICORN_LOG_LEVEL", "debug")
    monkeypatch.setattr(munchy_app.uvicorn, "run", run)
    monkeypatch.setattr(av1.uvicorn, "run", run)

    assert munchy_app.main([]) == 0
    av1.main([])

    assert [
        (application, values["host"], values["port"], values["log_level"])
        for application, values in calls
    ] == [
        ("munchy_api.app:app", "127.0.0.42", 9123, "debug"),
        ("munchy_av1_nvenc.main:app", "127.0.0.42", 9123, "debug"),
    ]
