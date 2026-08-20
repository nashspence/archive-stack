from __future__ import annotations

import json
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from riverhog_protocol import canonical_json_bytes
from stove0_extensions import MediaSamplingObserver, source_artifacts
from stove0_extensions import app as extension_app
from stove0_extensions.app import create_app
from stove0_extensions.target_service import (
    LocalMediaTargetService,
    PersistentTargetService,
    TargetExecutionCanceled,
    TargetExecutionInapplicable,
)
from stove0_media_contracts import PRESERVE_OPERATION, SOURCE_ROLE
from stove0_protocol import (
    ArtifactSubject,
    CollectionRootRef,
    ControllerEvidence,
    ControllerEvidencePayload,
    ExecutionEnvelope,
    ExecutionEnvelopePayload,
    JsonSchemaDocument,
    ObservationRequest,
    ObservationRequestPayload,
    OperationRef,
    RecipeRef,
    TargetPlanBinding,
    WorkflowPlan,
    WorkflowPlanPayload,
    WorkIdentity,
    WorkPayload,
)
from stove0_target_support import (
    InputArtifact,
    TargetCancelRequest,
    TargetContract,
    TargetContractPayload,
    TargetJobDeclaration,
    TargetJobRequest,
    TargetJobStatus,
    TargetOperationSupport,
    TargetProgress,
    TargetRuntimeAuthority,
    TransformPlan,
    TransformPlanPayload,
)


def _sha(character: str) -> str:
    return character * 64


def _target_request(token: str = "secret-capability") -> tuple[TargetContract, TargetJobRequest]:
    target = TargetContract.seal(
        TargetContractPayload(
            implementation_id="fixture.preserve-target/v1",
            implementation_version="1.0.0",
            source_revision="fixture",
            operations=(
                TargetOperationSupport(
                    operation_id=PRESERVE_OPERATION.id,
                    operation_contract_sha256=PRESERVE_OPERATION.contract_sha256,
                    options_schema=JsonSchemaDocument.from_schema(
                        "fixture.preserve-options/v1",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
            ),
        )
    )
    root = CollectionRootRef(
        collection_id=1,
        manifest_sha256=_sha("1"),
        content_etag=_sha("2"),
    )
    source = InputArtifact(
        id="source",
        role=SOURCE_ROLE,
        collection=root,
        path="camera/input.mov",
        bytes=12,
        sha256=_sha("3"),
        media_type="video/quicktime",
    )
    work = WorkIdentity.seal(
        WorkPayload(
            recipe=RecipeRef(id="fixture.preserve/v1", revision=1, sha256=_sha("4")),
            inputs=(root,),
        )
    )
    workflow = WorkflowPlan.seal(
        WorkflowPlanPayload(
            work=work,
            operation=OperationRef(
                id=PRESERVE_OPERATION.id,
                sha256=PRESERVE_OPERATION.contract_sha256,
            ),
            target_registration_id="fixture-target",
            target_contract_sha256=target.contract_sha256,
            output_tags=("preserved",),
            retirement_policy="retain",
        )
    )
    plan = TransformPlan.seal(
        TransformPlanPayload(
            target_implementation_id=target.implementation_id,
            target_contract_sha256=target.contract_sha256,
            operation_id=PRESERVE_OPERATION.id,
            operation_contract_sha256=PRESERVE_OPERATION.contract_sha256,
            inputs=(source,),
            intent={},
            target_options={},
        )
    )
    binding = TargetPlanBinding(
        protocol=target.protocol,
        target_implementation_id=target.implementation_id,
        target_contract_sha256=target.contract_sha256,
        operation_contract_sha256=PRESERVE_OPERATION.contract_sha256,
        plan=plan.binding_document(),
        plan_sha256=plan.plan_sha256,
    )
    envelope = ExecutionEnvelope.seal(
        ExecutionEnvelopePayload(
            claim_id=work.work_id,
            fence=1,
            workflow_plan=workflow,
            target_plan=binding,
        )
    )
    evidence = ControllerEvidence.seal(ControllerEvidencePayload(execution_envelope=envelope))
    declaration = TargetJobDeclaration(
        job_id=envelope.execution_envelope_sha256,
        claim_id=work.work_id,
        fence=1,
        controller_evidence=evidence,
        plan=plan,
        workspace_assurance="ephemeral",
    )
    return target, TargetJobRequest.seal(
        declaration,
        TargetRuntimeAuthority(
            riverhog_base_url="https://riverhog.invalid",
            capability_token=token,
        ),
    )


def _write_model(path: Path, value: Any) -> None:
    path.write_bytes(
        canonical_json_bytes(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    )


def test_target_restart_resumes_identical_declaration_without_persisting_token(
    tmp_path: Path,
) -> None:
    target, request = _target_request()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    job_id = request.declaration.job_id
    _write_model(state_root / f"{job_id}.accepted.json", request.accepted())
    _write_model(
        state_root / f"{job_id}.status.json",
        TargetJobStatus(
            job_id=job_id,
            state="running",
            attempt=1,
            request_sha256=request.request_sha256,
            plan_sha256=request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="transforming", completed=0),
        ),
    )

    def execute(
        _request: TargetJobRequest,
        _attempt: int,
        _cancellation: Any,
    ) -> TargetJobStatus:
        raise TargetExecutionInapplicable("unsupported-content", "fixture input")

    service = PersistentTargetService(
        contract=target,
        operations={PRESERVE_OPERATION.id: PRESERVE_OPERATION},
        state_root=state_root,
        execute=execute,
    )
    try:
        assert service.get_job(job_id).state == "interrupted"
        queued = service.put_job(request)
        assert queued.attempt == 2
        deadline = time.monotonic() + 5
        while service.get_job(job_id).state not in {"inapplicable", "failed"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        terminal = service.get_job(job_id)
        assert terminal.state == "inapplicable"
        assert terminal.inapplicable is not None
        assert terminal.inapplicable.code == "unsupported-content"
        assert service.put_job(request) == terminal
    finally:
        service.close()

    persisted = b"\n".join(path.read_bytes() for path in state_root.iterdir())
    assert b"secret-capability" not in persisted
    assert b"riverhog.invalid" not in persisted


def test_target_shutdown_interrupts_active_job_for_identical_restart(tmp_path: Path) -> None:
    target, request = _target_request()
    state_root = tmp_path / "state"
    started = threading.Event()

    def block_until_interrupted(
        _request: TargetJobRequest,
        _attempt: int,
        cancellation: threading.Event,
    ) -> TargetJobStatus:
        started.set()
        assert cancellation.wait(timeout=5)
        raise TargetExecutionCanceled

    service = PersistentTargetService(
        contract=target,
        operations={PRESERVE_OPERATION.id: PRESERVE_OPERATION},
        state_root=state_root,
        execute=block_until_interrupted,
    )
    service.put_job(request)
    assert started.wait(timeout=5)
    service.close()
    assert service.get_job(request.declaration.job_id).state == "interrupted"

    def finish_after_restart(
        _restarted_request: TargetJobRequest,
        _attempt: int,
        _cancellation: threading.Event,
    ) -> TargetJobStatus:
        raise TargetExecutionInapplicable("fixture-inapplicable", "fixture restart")

    restarted = PersistentTargetService(
        contract=target,
        operations={PRESERVE_OPERATION.id: PRESERVE_OPERATION},
        state_root=state_root,
        execute=finish_after_restart,
    )
    try:
        queued = restarted.put_job(request)
        assert queued.attempt == 2
        deadline = time.monotonic() + 5
        while restarted.get_job(request.declaration.job_id).state != "inapplicable":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        restarted.close()


def test_operator_cancel_wins_completion_race(tmp_path: Path) -> None:
    target, request = _target_request()
    started = threading.Event()
    release = threading.Event()

    def complete_after_cancel(
        active_request: TargetJobRequest,
        attempt: int,
        _cancellation: threading.Event,
    ) -> TargetJobStatus:
        started.set()
        assert release.wait(timeout=5)
        return TargetJobStatus(
            job_id=active_request.declaration.job_id,
            state="succeeded",
            attempt=attempt,
            request_sha256=active_request.request_sha256,
            plan_sha256=active_request.declaration.plan.plan_sha256,
            progress=TargetProgress(phase="complete", completed=1, total=1),
            outputs=(),
        )

    service = PersistentTargetService(
        contract=target,
        operations={PRESERVE_OPERATION.id: PRESERVE_OPERATION},
        state_root=tmp_path / "state",
        execute=complete_after_cancel,
    )
    try:
        service.put_job(request)
        assert started.wait(timeout=5)
        canceled = service.cancel_job(
            request.declaration.job_id,
            TargetCancelRequest(reason="operator"),
        )
        assert canceled.state == "canceling"
        release.set()
        deadline = time.monotonic() + 5
        while service.get_job(request.declaration.job_id).state != "canceled":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        release.set()
        service.close()


def test_target_command_log_is_removed_after_tool_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    service = LocalMediaTargetService(
        state_root=tmp_path / "state",
        workspace_root=tmp_path / "work",
    )

    class FailedProcess:
        returncode = 7

        def poll(self) -> int:
            return self.returncode

    def popen(_command: list[str], **kwargs: Any) -> FailedProcess:
        kwargs["stderr"].write(b"fixture tool failure")
        kwargs["stderr"].flush()
        return FailedProcess()

    monkeypatch.setattr("stove0_extensions.target_service.subprocess.Popen", popen)
    try:
        with pytest.raises(TargetExecutionInapplicable, match="fixture tool"):
            service._command(  # noqa: SLF001
                ["fixture-tool"],
                timeout=1,
                check=lambda: None,
                invalid_code="fixture-inapplicable",
            )
        assert tuple((tmp_path / "work").glob(".command-*.log")) == ()
    finally:
        service.close()


def test_extension_readiness_checks_the_configured_runtime_tool(
    monkeypatch: Any,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setenv("STOVE0_FFPROBE_BIN", "/opt/stove0/bin/ffprobe")

    def run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("stove0_extensions.app.subprocess.run", run)
    with TestClient(create_app(mode="observer", token="fixture-token")) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert commands == [["/opt/stove0/bin/ffprobe", "-version"]]


def test_extension_environment_connects_process_and_implementation_settings(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "extension.token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("STOVE0_EXTENSION_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("STOVE0_EXTENSION_TOKEN", raising=False)
    assert extension_app._secret() == "file-token"

    monkeypatch.setenv("STOVE0_EXTENSION_TOKEN", "direct-token")
    monkeypatch.delenv("STOVE0_EXTENSION_TOKEN_FILE", raising=False)
    assert extension_app._secret() == "direct-token"

    monkeypatch.setenv("STOVE0_EXTENSION_HOST", "0.0.0.0")
    monkeypatch.setenv("STOVE0_EXTENSION_PORT", "8099")
    parsed = extension_app._parser().parse_args(["observer"])
    assert (parsed.host, parsed.port) == ("0.0.0.0", 8099)

    monkeypatch.setenv("STOVE0_EXTENSION_SOURCE_REVISION", "fixture-revision")
    assert MediaSamplingObserver().descriptor().source_revision == "fixture-revision"

    monkeypatch.setenv("STOVE0_TARGET_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("STOVE0_EXTENSION_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("STOVE0_FFMPEG_BIN", "fixture-ffmpeg")
    target = extension_app._target_from_environment("local-target")
    try:
        assert target.ffmpeg == "fixture-ffmpeg"  # type: ignore[attr-defined]
        assert target.contract().source_revision == "fixture-revision"
    finally:
        target.close()

    monkeypatch.setenv("STOVE0_SOURCE_ARTIFACT_ZSTD", "fixture-zstd")
    monkeypatch.setattr(source_artifacts.shutil, "which", lambda command: f"/bin/{command}")
    assert source_artifacts._zstd_command() == "/bin/fixture-zstd"


class _Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.released = False

    def release(self) -> None:
        self.released = True


class _ObserverRuntime:
    def __init__(self, root: Path) -> None:
        self.workspace = _Workspace(root)
        self.heartbeats = 0

    def open_workspace(self, _root: Path) -> _Workspace:
        return self.workspace

    def heartbeat(self) -> None:
        self.heartbeats += 1

    def materialize(
        self,
        _subject: ArtifactSubject,
        *,
        workspace: _Workspace,
        relative_path: str,
    ) -> Path:
        path = workspace.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"immutable-media")
        return path


def test_media_observer_returns_bounded_contract_facts_without_mutating_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    observer = MediaSamplingObserver(ffprobe="fixture-ffprobe")
    descriptor = observer.descriptor()
    contract = descriptor.contracts[0]
    request = ObservationRequest.seal(
        ObservationRequestPayload(
            work_id=_sha("a"),
            observer_registration_id="media-sampling",
            observer_descriptor_sha256=descriptor.descriptor_sha256,
            observer_contract_id=contract.contract_id,
            observer_contract_sha256=contract.contract_sha256,
            subjects=(
                ArtifactSubject(
                    id="camera-source",
                    role=SOURCE_ROLE,
                    collection=CollectionRootRef(
                        collection_id=1,
                        manifest_sha256=_sha("1"),
                        content_etag=_sha("2"),
                    ),
                    path="camera/input.mov",
                    bytes=15,
                    sha256=_sha("3"),
                ),
            ),
            options={},
            timeout_seconds=10,
            maximum_result_bytes=4096,
        )
    )
    runtime = _ObserverRuntime(tmp_path / "work")
    monkeypatch.setenv("STOVE0_EXTENSION_WORKSPACE", str(tmp_path / "workspace-root"))

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        if "-version" in command:
            return SimpleNamespace(returncode=0, stdout="ffprobe fixture\n", stderr="")
        assert kwargs["check"] is False
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"format": {"duration": "12.5"}, "streams": [{"duration": "12.4"}]}
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("stove0_extensions.observer.subprocess.run", run)
    result = observer.observe(request, runtime)  # type: ignore[arg-type]

    assert result.state == "observed"
    assert result.facts == {
        "artifacts": [
            {
                "artifact_id": "camera-source",
                "duration_ms": 12500,
                "sampleable_ranges": [{"start_ms": 0, "duration_ms": 12500}],
            }
        ]
    }
    assert runtime.heartbeats == 1
    assert runtime.workspace.released is True
    assert (tmp_path / "work" / "input" / "camera-source").read_bytes() == b"immutable-media"


def test_source_artifact_bundle_is_canonical_audited_stove0_v1(tmp_path: Path) -> None:
    work = tmp_path / "work"
    archive = tmp_path / "clip.mkv"
    archive.write_bytes(b"archive")
    artifacts = source_artifacts._assemble_source_artifact_bundle_inputs(
        work_dir=work,
        src="clip.mp4",
        output=archive.name,
        source_metadata={"format": {"format_name": "mov"}, "streams": []},
        source_container={"supported": True, "mode": "iso_bmff_rebuild"},
        container_inventory=[],
        container_artifacts=[],
        exports=[],
        stream_transforms=[],
        dropped_items=[],
        encode_cmd=["ffmpeg", "-i", "{source}", "{archive}"],
        selected_output_path=archive,
        encode_output_path=archive,
    )
    first = tmp_path / "first.source-artifacts.tar"
    second = tmp_path / "second.source-artifacts.tar"

    assert source_artifacts._build_source_artifacts_bundle(
        first,
        artifacts,
        src="clip.mp4",
        output=archive.name,
    )
    assert source_artifacts._build_source_artifacts_bundle(
        second,
        artifacts,
        src="clip.mp4",
        output=archive.name,
    )

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r") as bundle:
        member = bundle.extractfile("manifest.json")
        assert member is not None
        manifest = json.loads(member.read())
        assert manifest["kind"] == "stove0.media.source-artifacts/v1"
        assert all(item.mtime == 0 for item in bundle.getmembers())
    audit = source_artifacts._audit_source_artifacts_bundle(first)
    assert audit["ok"] is True
    assert audit["rebuild_supported"] is True
    assert audit["artifacts_checked"] == len(artifacts)
