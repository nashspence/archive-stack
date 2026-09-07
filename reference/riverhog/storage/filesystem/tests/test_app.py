from __future__ import annotations

from pathlib import Path

import pytest
from riverhog_storage_adapter_filesystem import app


def test_config_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_ROOT",
        str(tmp_path / "cache"),
    )
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_SEGMENT_BYTES", "128KiB")
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_READ_CHUNK_BYTES", "1MiB")
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_MINIMUM_FREE_BYTES", "0B")

    config = app.config_from_environment()

    assert config.root == tmp_path / "cache"
    assert config.segment_bytes == 128 * 1024
    assert config.read_chunk_bytes == 1024**2
    assert config.minimum_free_bytes == 0


def test_root_must_be_absolute_non_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_ROOT", "/")
    with pytest.raises(ValueError, match="absolute non-root"):
        app.config_from_environment()


def test_secret_requires_exactly_one_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_TOKEN", raising=False)
    monkeypatch.delenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match="exactly one"):
        app._secret("TOKEN")

    token_file = tmp_path / "token"
    token_file.write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv(
        "RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_TOKEN_FILE",
        str(token_file),
    )
    assert app._secret("TOKEN") == "secret"


def test_main_wires_asgi_service_and_releases_root_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_ROOT", str(root))
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_TOKEN", "test-token")
    monkeypatch.setenv("RIVERHOG_FILESYSTEM_STORAGE_ADAPTER_MINIMUM_FREE_BYTES", "0B")
    captured: dict[str, object] = {}
    sentinel = object()

    def create_service(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    def run_service(service: object, *, host: str, port: int) -> None:
        assert service is sentinel
        assert host == "127.0.0.1"
        assert port == 8765

    monkeypatch.setattr(app, "create_storage_adapter_app", create_service)
    monkeypatch.setattr(app.uvicorn, "run", run_service)

    assert app.main(["--port", "8765"]) == 0
    assert captured["service"] == app.SERVICE
    assert captured["token"] == "test-token"
    adapter = captured["adapter"]
    with pytest.raises(RuntimeError, match="closed"):
        adapter.readiness()  # type: ignore[attr-defined]

    # Closing the service must release its exclusive root lock.
    from riverhog_storage_adapter_filesystem import (
        FilesystemStorageAdapter,
        FilesystemStorageAdapterConfig,
    )

    with FilesystemStorageAdapter(FilesystemStorageAdapterConfig(root=root, minimum_free_bytes=0)):
        pass
