from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient
from stove0_ffprobe_sampling_observer import FfprobeSamplingObserver
from stove0_ffprobe_sampling_observer import app as observer_app
from stove0_ffprobe_sampling_observer.app import create_app
from stove0_observer_support import ObservationRuntime
from stove0_protocol import (
    ArtifactSubject,
    CollectionRootRef,
    ObservationRequest,
    ObservationRequestPayload,
)
from stove0_review_contracts import MEDIA_SAMPLING_OBSERVER_CONTRACT


def _sha(character: str) -> str:
    return character * 64


class FixtureWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, parents=True)
        self.released = False

    def resolve(self, relative_path: str) -> Path:
        return self.root.joinpath(*relative_path.split("/"))

    def release(self) -> None:
        self.released = True


class FixtureRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace: FixtureWorkspace | None = None
        self.heartbeats = 0

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def open_workspace(self, _root: Path) -> FixtureWorkspace:
        self.workspace = FixtureWorkspace(self.root)
        return self.workspace

    def materialize(
        self,
        _subject: ArtifactSubject,
        *,
        workspace: FixtureWorkspace,
        relative_path: str,
    ) -> Path:
        destination = workspace.resolve(relative_path)
        destination.parent.mkdir(mode=0o700, parents=True)
        destination.write_bytes(b"immutable-media")
        return destination


def test_ffprobe_observer_reports_contract_facts_and_exact_image(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    observer = FfprobeSamplingObserver(
        ffprobe="fixture-ffprobe",
        workspace_root=tmp_path / "observer-workspace",
        source_revision="fixture",
        image_digest=_sha("9"),
    )
    descriptor = observer.descriptor()
    support = descriptor.contracts[0]
    request = ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=_sha("1"),
            observer_registration_id="ffprobe-sampling",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=support.contract_id,
            observer_contract_sha256=support.contract_sha256,
            subjects=(
                ArtifactSubject(
                    id="camera-source",
                    role="stove0.review.source/v1",
                    collection=CollectionRootRef(
                        collection_id=1,
                        manifest_sha256=_sha("2"),
                        content_etag=_sha("3"),
                    ),
                    path="camera/source.mp4",
                    bytes=15,
                    sha256=_sha("4"),
                    media_type="video/mp4",
                ),
            ),
            maximum_result_bytes=256 * 1024,
        )
    )

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if "-version" in command:
            return SimpleNamespace(returncode=0, stdout="ffprobe fixture\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"format": {"duration": "12.5"}, "streams": [{"duration": "12.4"}]}
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(
        "stove0_ffprobe_sampling_observer.observer.subprocess.run",
        run,
    )
    runtime = FixtureRuntime(tmp_path / "request")
    result = observer.observe(request, cast(ObservationRuntime, runtime))

    assert descriptor.image_digest == _sha("9")
    assert support.contract_id == MEDIA_SAMPLING_OBSERVER_CONTRACT.id
    assert result.state == "observed"
    assert result.facts == {
        "artifacts": [
            {
                "artifact_id": "camera-source",
                "duration_ms": 12500,
                "sampleable_ranges": [
                    {"start_ms": 0, "duration_ms": 12500},
                ],
            }
        ]
    }
    assert runtime.heartbeats == 1
    assert runtime.workspace is not None and runtime.workspace.released


def test_observer_process_exposes_only_observer_contract() -> None:
    observer = FfprobeSamplingObserver(
        source_revision="fixture",
        image_digest=_sha("9"),
    )
    client = TestClient(create_app(token="observer-secret", observer=observer))
    response = client.get(
        "/v1/observer",
        headers={"Authorization": "Bearer observer-secret"},
    )
    assert response.status_code == 200
    assert response.json()["implementation_id"] == "stove0.ffprobe-sampling-observer/v1"
    assert (
        client.get(
            "/v1/target",
            headers={"Authorization": "Bearer observer-secret"},
        ).status_code
        == 404
    )


def test_observer_process_environment_is_connected(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    token_file = tmp_path / "observer.token"
    token_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN", raising=False)
    assert observer_app._secret() == "file-secret"
    monkeypatch.delenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN_FILE")
    monkeypatch.setenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_TOKEN", "direct-secret")
    monkeypatch.setenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_HOST", "127.0.0.7")
    monkeypatch.setenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_PORT", "8177")
    monkeypatch.setenv("STOVE0_FFPROBE_BIN", "fixture-ffprobe")
    monkeypatch.setenv(
        "STOVE0_FFPROBE_SAMPLING_OBSERVER_WORKSPACE",
        str(tmp_path / "workspace"),
    )
    monkeypatch.setenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_SOURCE_REVISION", "fixture-revision")
    monkeypatch.setenv("STOVE0_FFPROBE_SAMPLING_OBSERVER_IMAGE_DIGEST", _sha("8"))
    created: dict[str, object] = {}

    class ConfiguredObserver:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)
            self.ffprobe = str(kwargs["ffprobe"])

    def run(_app: object, *, host: str, port: int) -> None:
        created["host"] = host
        created["port"] = port

    monkeypatch.setattr(observer_app, "FfprobeSamplingObserver", ConfiguredObserver)
    monkeypatch.setattr(observer_app.uvicorn, "run", run)

    assert observer_app.main([]) == 0
    assert created == {
        "ffprobe": "fixture-ffprobe",
        "workspace_root": tmp_path / "workspace",
        "source_revision": "fixture-revision",
        "image_digest": _sha("8"),
        "host": "127.0.0.7",
        "port": 8177,
    }
