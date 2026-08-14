from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import jeb_core.adapters.munchy as munchy_adapter_module
from fastapi.testclient import TestClient
from jeb_core.adapters.munchy import MunchyTargetAdapter
from jeb_core.domain.models import EligibleFile, TargetConfig
from jeb_core.domain.sources import SourceConfig
from munchy_api_client.client import MunchyClient


def load_munchy_server(tmp_path: Path, monkeypatch) -> ModuleType:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MUNCHY_APPLICATION_AUTH_REQUIRED", "0")
    monkeypatch.setenv("MUNCHY_STATE_DIR", str(tmp_path / "munchy-state"))
    monkeypatch.setenv("MUNCHY_WORK_DIR", str(tmp_path / "munchy-work"))
    monkeypatch.setenv("MUNCHY_TUSD_DIR", str(tmp_path / "munchy-tusd"))
    monkeypatch.setenv("MUNCHY_GPU_RUNTIME_DIR", str(tmp_path / "munchy-gpu-runtime"))
    for module_name in tuple(sys.modules):
        if module_name == "munchy_api.app" or module_name.startswith("munchy_core."):
            sys.modules.pop(module_name, None)
    server = importlib.import_module("munchy_api.app")
    persistence = importlib.import_module("munchy_core.persistence")
    persistence.initialize_persistence()
    return server


def job_template_definition() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "munchy.job",
        "job": {
            "workflow_mode": "collection_archive",
            "handoff": {
                "destination": "riverhog",
                "options": {"archive_store": "test"},
            },
        },
    }


def test_jeb_preflight_is_accepted_by_the_running_munchy_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    server = load_munchy_server(tmp_path, monkeypatch)

    with TestClient(server.app) as http:
        created = http.post(
            "/v1/admin/job-templates",
            json={
                "template_id": "camera-archive",
                "definition": job_template_definition(),
            },
        )
        assert created.status_code == 201, created.json()

        class InProcessHttp:
            def request(self, method: str, url: str, **kwargs: object):  # type: ignore[no-untyped-def]
                kwargs.pop("timeout", None)
                return http.request(method, url, **kwargs)

            def close(self) -> None:
                return

        class InProcessMunchyClient(MunchyClient):
            def __init__(
                self,
                _url: str,
                *,
                token: str = "",
                allow_insecure_http: bool = False,
            ) -> None:
                super().__init__(
                    "https://munchy.test",
                    token=token,
                    allow_insecure_http=allow_insecure_http,
                )
                self._http.close()
                self._http = InProcessHttp()  # type: ignore[assignment]

            def close(self) -> None:
                return

        class Context:
            def target_by_name(self, name: str) -> TargetConfig:
                assert name == "munchy"
                return TargetConfig(name="munchy", url="https://munchy.test")

            def record_target_preflight_failure(self, **_kwargs: object) -> None:
                raise AssertionError("accepted preflight recorded a failure")

            def clear_target_preflight_failure(self, source_id: str) -> None:
                assert source_id == "camera"

            def emit_target_preflight_failures(self, **_kwargs: object) -> None:
                raise AssertionError("accepted preflight emitted a failure")

        monkeypatch.setattr(munchy_adapter_module, "MunchyClient", InProcessMunchyClient)
        source = SourceConfig(
            id="camera",
            enabled=True,
            path=tmp_path / "landing" / "camera",
            adapters=("tus",),
            stable_seconds=0,
            include_extensions=frozenset({".mp4"}),
            target="munchy",
            target_config={"template_id": "camera-archive"},
            threshold_bytes=0,
            cleanup="never",
            cadence="manual",
            weekday=0,
            hour=0,
            minute=0,
        )
        eligible = EligibleFile(
            path=source.path / "clip.mp4",
            rel=Path("clip.mp4"),
            target_path="camera/clip.mp4",
            bytes=5,
            mtime=1.0,
            mtime_ns=1,
            device=1,
            inode=1,
        )

        accepted, summary = MunchyTargetAdapter().preflight(
            Context(),  # type: ignore[arg-type]
            source,
            (eligible,),
            record_failures=True,
        )

    assert accepted == [eligible]
    assert summary["status"] == "accepted"
    assert summary["result"]["files_total"] == 1
    assert summary["result"]["bytes_total"] == 5
