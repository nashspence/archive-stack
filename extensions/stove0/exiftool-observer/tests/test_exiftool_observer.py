from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient
from stove0_exiftool_observer import ExiftoolObserver
from stove0_exiftool_observer import app as observer_app
from stove0_exiftool_observer.app import create_app
from stove0_media_archive_contracts import (
    MEDIA_METADATA_OBSERVER_CONTRACT,
    MediaMetadataFacts,
)
from stove0_observer_support import ObservationRuntime
from stove0_protocol import (
    ArtifactSubject,
    CollectionRootRef,
    ObservationRequest,
    ObservationRequestPayload,
)


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


def _request(observer: ExiftoolObserver) -> ObservationRequest:
    descriptor = observer.descriptor()
    support = descriptor.contracts[0]
    root = CollectionRootRef(
        collection_id=1,
        manifest_sha256=_sha("2"),
        content_etag=_sha("3"),
    )
    return ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=_sha("1"),
            observer_registration_id="exiftool",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=support.contract_id,
            observer_contract_sha256=support.contract_sha256,
            subjects=(
                ArtifactSubject(
                    id="camera-primary",
                    role="stove0.media.source/v1",
                    collection=root,
                    path="camera/clip.mov",
                    bytes=15,
                    sha256=_sha("4"),
                    media_type="video/quicktime",
                ),
                ArtifactSubject(
                    id="camera-sidecar",
                    role="stove0.media.source/v1",
                    collection=root,
                    path="camera/clip.xmp",
                    bytes=15,
                    sha256=_sha("5"),
                    media_type="application/rdf+xml",
                ),
            ),
            maximum_result_bytes=512 * 1024,
        )
    )


def test_exiftool_observer_preserves_conflicting_exact_field_evidence(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    observer = ExiftoolObserver(
        exiftool="fixture-exiftool",
        workspace_root=tmp_path / "observer-workspace",
        source_revision="fixture",
        image_digest=_sha("9"),
    )

    probe_commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if "-ver" in command:
            return SimpleNamespace(returncode=0, stdout="13.59\n", stderr="")
        probe_commands.append(command)
        sources = tuple(Path(value) for value in command if value.startswith(str(tmp_path)))
        documents: list[dict[str, object]] = []
        for source in sources:
            if source.suffix == ".xmp":
                documents.append(
                    {
                        "SourceFile": str(source),
                        "XMP-xmp:CreateDate": "2025:02:03 04:05:06-08:00",
                        "XMP-dc:Creator": ["Alex Example", "River Example"],
                        "XMP-tiff:Make": "Sidecar Camera Corp",
                    }
                )
            else:
                documents.append(
                    {
                        "SourceFile": str(source),
                        "EXIF:DateTimeOriginal": "2025:02:03 04:05:01",
                        "QuickTime:CreateDate": "2025:02:03 12:05:01",
                        "QuickTime:GPSCoordinates": "+45.5000-122.6000/",
                        "QuickTime:GPSLatitude": 45.5,
                        "QuickTime:GPSLongitude": -122.6,
                        "EXIF:Make": "Camera Corp",
                        "EXIF:Model": "Camera One",
                        "File:MIMEType": "video/quicktime",
                    }
                )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(documents).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("stove0_exiftool_observer.observer.subprocess.run", run)
    runtime = FixtureRuntime(tmp_path / "request")
    result = observer.observe(_request(observer), cast(ObservationRuntime, runtime))
    facts = MediaMetadataFacts.model_validate(result.facts)

    assert observer.descriptor().image_digest == _sha("9")
    assert observer.descriptor().contracts[0].contract_id == MEDIA_METADATA_OBSERVER_CONTRACT.id
    assert result.state == "observed"
    assert [item.artifact_id for item in facts.artifacts] == [
        "camera-primary",
        "camera-sidecar",
    ]
    primary = facts.artifacts[0]
    sidecar = facts.artifacts[1]
    assert primary.state == "observed"
    assert [(fact.name, fact.value, fact.evidence.field) for fact in primary.facts] == [
        ("capture-time", "2025:02:03 04:05:01", "EXIF:DateTimeOriginal"),
        ("capture-time", "2025:02:03 12:05:01", "QuickTime:CreateDate"),
        ("container-format", "video/quicktime", "File:MIMEType"),
        ("device-make", "Camera Corp", "EXIF:Make"),
        ("device-model", "Camera One", "EXIF:Model"),
        ("gps-latitude", 45.5, "QuickTime:GPSCoordinates"),
        ("gps-latitude", 45.5, "QuickTime:GPSLatitude"),
        ("gps-longitude", -122.6, "QuickTime:GPSCoordinates"),
        ("gps-longitude", -122.6, "QuickTime:GPSLongitude"),
    ]
    assert [(fact.name, fact.value, fact.evidence.field) for fact in sidecar.facts] == [
        ("capture-time", "2025:02:03 04:05:06-08:00", "XMP-xmp:CreateDate"),
        ("creator", ["Alex Example", "River Example"], "XMP-dc:Creator"),
        ("device-make", "Sidecar Camera Corp", "XMP-tiff:Make"),
    ]
    assert all(
        fact.evidence.artifact_id == artifact.artifact_id
        for artifact in facts.artifacts
        for fact in artifact.facts
    )
    assert runtime.heartbeats == 2
    assert runtime.workspace is not None and runtime.workspace.released
    assert len(probe_commands) == 1
    assert "-G1" in probe_commands[0]
    assert "-GPSLatitude" in probe_commands[0]


def test_observer_process_exposes_only_media_metadata_observer_contract() -> None:
    observer = ExiftoolObserver(source_revision="fixture", image_digest=_sha("9"))
    client = TestClient(create_app(token="observer-secret", observer=observer))

    response = client.get(
        "/v1/observer",
        headers={"Authorization": "Bearer observer-secret"},
    )

    assert response.status_code == 200
    assert response.json()["implementation_id"] == "stove0.exiftool-observer/v1"
    assert response.json()["contracts"][0]["contract_id"] == (MEDIA_METADATA_OBSERVER_CONTRACT.id)
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
    monkeypatch.setenv("STOVE0_EXIFTOOL_OBSERVER_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("STOVE0_EXIFTOOL_OBSERVER_TOKEN", raising=False)
    assert observer_app._secret() == "file-secret"
    monkeypatch.delenv("STOVE0_EXIFTOOL_OBSERVER_TOKEN_FILE")
    monkeypatch.setenv("STOVE0_EXIFTOOL_OBSERVER_TOKEN", "direct-secret")
    monkeypatch.setenv("STOVE0_EXIFTOOL_OBSERVER_HOST", "127.0.0.7")
    monkeypatch.setenv("STOVE0_EXIFTOOL_OBSERVER_PORT", "8177")
    monkeypatch.setenv("STOVE0_EXIFTOOL_BIN", "fixture-exiftool")
    monkeypatch.setenv(
        "STOVE0_EXIFTOOL_OBSERVER_WORKSPACE",
        str(tmp_path / "workspace"),
    )
    monkeypatch.setenv("STOVE0_EXIFTOOL_OBSERVER_SOURCE_REVISION", "fixture-revision")
    monkeypatch.setenv("STOVE0_EXIFTOOL_OBSERVER_IMAGE_DIGEST", _sha("8"))
    created: dict[str, object] = {}

    class ConfiguredObserver:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)
            self.exiftool = str(kwargs["exiftool"])

    def run(_app: object, *, host: str, port: int) -> None:
        created["host"] = host
        created["port"] = port

    monkeypatch.setattr(observer_app, "ExiftoolObserver", ConfiguredObserver)
    monkeypatch.setattr(observer_app.uvicorn, "run", run)

    assert observer_app.main([]) == 0
    assert created == {
        "exiftool": "fixture-exiftool",
        "workspace_root": tmp_path / "workspace",
        "source_revision": "fixture-revision",
        "image_digest": _sha("8"),
        "host": "127.0.0.7",
        "port": 8177,
    }
