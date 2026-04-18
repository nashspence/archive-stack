from __future__ import annotations

import importlib
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis

LOCAL_AGE_CLI = Path("/tmp/age-bin/age/age")
REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "api"

if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

DEFAULT_ENV = {
    "API_BASE_URL": "http://archive.test",
    "API_TOKEN": "test-api-token",
    "AGE_BATCHPASS_PASSPHRASE": os.environ.get("AGE_BATCHPASS_PASSPHRASE", "test-batchpass-passphrase"),
    "AGE_BATCHPASS_WORK_FACTOR": os.environ.get("AGE_BATCHPASS_WORK_FACTOR", "2"),
    "AGE_BATCHPASS_MAX_WORK_FACTOR": os.environ.get("AGE_BATCHPASS_MAX_WORK_FACTOR", "2"),
    "AGE_CLI": os.environ.get("AGE_CLI", str(LOCAL_AGE_CLI) if LOCAL_AGE_CLI.exists() else "age"),
    "HOOK_SECRET": "test-hook-secret",
    "OTS_CLIENT_COMMAND": f"python {API_ROOT / 'app' / 'ots_stub.py'}",
    "CONTAINER_BUFFER_MAX_GB": "0.0100",
    "CONTAINER_FILL_GB": "0.0015",
    "CONTAINER_SPILL_FILL_GB": "0.0010",
    "CONTAINER_TARGET_GB": "0.0025",
    "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
    "TUSD_BASE_URL": "http://tusd.test/files",
}


def _reset_app_modules() -> None:
    db_module = sys.modules.get("app.db")
    if db_module is not None:
        db_module.engine.dispose()

    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            sys.modules.pop(module_name, None)


@dataclass
class LoadedModules:
    config: ModuleType
    db: ModuleType
    main: ModuleType
    models: ModuleType
    notifications: ModuleType
    progress: ModuleType
    storage: ModuleType
    archive_root: Path
    sqlite_path: Path
    env: dict[str, str]


@dataclass
class AppHarness:
    client: TestClient
    modules: LoadedModules

    @property
    def archive_root(self) -> Path:
        return self.modules.archive_root

    @property
    def sqlite_path(self) -> Path:
        return self.modules.sqlite_path

    @property
    def config(self) -> ModuleType:
        return self.modules.config

    @property
    def db(self) -> ModuleType:
        return self.modules.db

    @property
    def models(self) -> ModuleType:
        return self.modules.models

    @property
    def progress(self) -> ModuleType:
        return self.modules.progress

    @property
    def notifications(self) -> ModuleType:
        return self.modules.notifications

    @property
    def storage(self) -> ModuleType:
        return self.modules.storage

    @property
    def api_token(self) -> str:
        return self.modules.env["API_TOKEN"]

    @property
    def hook_secret(self) -> str:
        return self.modules.env["HOOK_SECRET"]

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    def hook_headers(self, hook_name: str) -> dict[str, str]:
        return {"Hook-Name": hook_name}

    def hook_url(self) -> str:
        return f"/internal/tusd-hooks?hook_secret={self.hook_secret}"

    def redis_flush(self) -> None:
        client = SyncRedis.from_url(self.modules.env["REDIS_URL"], decode_responses=True)
        try:
            client.flushdb()
        finally:
            client.close()

    def redis_messages(self, stream_name: str) -> list[tuple[str, dict[str, str]]]:
        client = SyncRedis.from_url(self.modules.env["REDIS_URL"], decode_responses=True)
        try:
            return client.xrange(stream_name)
        finally:
            client.close()

    @contextmanager
    def session(self) -> Iterator[Any]:
        session = self.db.SessionLocal()
        try:
            yield session
        finally:
            session.close()


def _load_modules_with_env(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    env_overrides: dict[str, str] | None = None,
    before_import: Callable[[dict[str, str], Path], None] | None = None,
) -> Iterator[LoadedModules]:
    base_dir = tmp_path_factory.mktemp("archive-suite")
    archive_root = base_dir / "archive"
    sqlite_path = archive_root / "catalog" / "catalog.sqlite3"

    env = dict(DEFAULT_ENV)
    env.update(
        {
            "ARCHIVE_ROOT": str(archive_root),
            "SQLITE_PATH": str(sqlite_path),
        }
    )
    if env_overrides:
        env.update({key: str(value) for key, value in env_overrides.items()})

    previous = {key: os.environ.get(key) for key in env}

    try:
        for key, value in env.items():
            os.environ[key] = value

        if before_import is not None:
            before_import(env, base_dir)

        _reset_app_modules()
        loaded = LoadedModules(
            config=importlib.import_module("app.config"),
            db=importlib.import_module("app.db"),
            main=importlib.import_module("app.main"),
            models=importlib.import_module("app.models"),
            notifications=importlib.import_module("app.notifications"),
            progress=importlib.import_module("app.progress"),
            storage=importlib.import_module("app.storage"),
            archive_root=Path(env["ARCHIVE_ROOT"]),
            sqlite_path=Path(env["SQLITE_PATH"]),
            env=env,
        )
        yield loaded
    finally:
        _reset_app_modules()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def module_factory(tmp_path_factory: pytest.TempPathFactory):
    @contextmanager
    def _factory(
        *,
        before_import: Callable[[dict[str, str], Path], None] | None = None,
        **env_overrides: str,
    ) -> Iterator[LoadedModules]:
        yield from _load_modules_with_env(
            tmp_path_factory,
            env_overrides=env_overrides,
            before_import=before_import,
        )

    return _factory


@pytest.fixture
def app_factory(module_factory):
    @contextmanager
    def _factory(
        *,
        before_import: Callable[[dict[str, str], Path], None] | None = None,
        **env_overrides: str,
    ) -> Iterator[AppHarness]:
        with module_factory(before_import=before_import, **env_overrides) as loaded:
            with TestClient(loaded.main.app) as client:
                harness = AppHarness(client=client, modules=loaded)
                harness.redis_flush()
                yield harness

    return _factory
