from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "riverhog_ftp_adapter.py"


def _module() -> object:
    spec = importlib.util.spec_from_file_location("riverhog_ftp_adapter", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("FTP adapter module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ftp_adapter_sources_setting_is_parsed_into_bounded_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    landing = tmp_path / "landing"
    monkeypatch.setenv(
        "RIVERHOG_FTP_ADAPTER_SOURCES",
        json.dumps(
            {
                "camera-a": {
                    "path": str(landing),
                    "tags": ["intake-camera"],
                    "ingest_source": "ftp:camera-a",
                    "stable_seconds": 12,
                    "max_files": 25,
                    "max_bytes": 4096,
                }
            }
        ),
    )

    module = _module()
    sources = module._sources_from_env()  # type: ignore[attr-defined]

    assert len(sources) == 1
    source = sources[0]
    assert source.id == "camera-a"
    assert source.root == landing.resolve()
    assert source.tags == ("intake-camera",)
    assert source.ingest_source == "ftp:camera-a"
    assert source.stable_seconds == 12
    assert source.max_files == 25
    assert source.max_bytes == 4096
