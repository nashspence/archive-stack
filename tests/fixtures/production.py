from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from arc_api.app import create_app
from arc_core.fs_paths import normalize_collection_id
from tests.fixtures.acceptance import REPO_ROOT, SRC_ROOT, _LiveServerHandle, _reserve_local_port
from tests.fixtures.data import PHOTOS_2024_FILES, write_tree


@dataclass(slots=True)
class ProductionSystem:
    workspace: Path
    server: _LiveServerHandle
    base_url: str

    @classmethod
    def create(cls, workspace: Path) -> ProductionSystem:
        app = create_app()
        with _reserve_local_port() as reserved:
            server = _LiveServerHandle(app, host="127.0.0.1", port=reserved.port)
        server.start()
        return cls(
            workspace=workspace,
            server=server,
            base_url=server.base_url,
        )

    def close(self) -> None:
        self.server.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
            return client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=headers,
                content=content,
            )

    def run_arc(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "arc_cli.main", *args],
            cwd=REPO_ROOT,
            env=self._subprocess_env(),
            capture_output=True,
            text=True,
            check=False,
        )

    def run_arc_disc(
        self, *args: str, input_text: str = "\n" * 16
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "arc_disc.main", *args],
            cwd=REPO_ROOT,
            env=self._subprocess_env(),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )

    def seed_staged_collection(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        write_tree(
            self.workspace / "staging" / normalized_collection_id, files or PHOTOS_2024_FILES
        )

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        pythonpath_parts = [str(ROOT) for ROOT in (SRC_ROOT, REPO_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
        return env

    def __getattr__(self, name: str) -> object:
        raise NotImplementedError(
            f"production acceptance suite does not provide fixture-backed state for {name}"
        )


@pytest.fixture
def acceptance_system(tmp_path: Path) -> Iterator[ProductionSystem]:
    system = ProductionSystem.create(tmp_path)
    try:
        yield system
    finally:
        system.close()
