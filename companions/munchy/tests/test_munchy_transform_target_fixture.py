from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from munchy_target_support.client import TransformTargetClient
from munchy_target_support.operations import (
    SOURCE_ROLE,
    VIDEO_ARCHIVE_OPERATION,
    VideoArchiveIntent,
    operation_contract,
)
from munchy_target_support.protocol import (
    Artifact,
    TargetCancelRequest,
    TargetJobRequest,
    TargetJobRequestPayload,
    TargetJobStatus,
    TargetPreflightRequest,
)
from munchy_target_support.workspace import workspace_area_root, workspace_artifact_path


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class TargetFixtureProcess:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _unused_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "munchy_transform_target.py"
        environment = {
            **os.environ,
            "MUNCHY_FIXTURE_ROOT": str(self.root),
            "MUNCHY_FIXTURE_PORT": str(self.port),
        }
        self.process = subprocess.Popen(
            [sys.executable, str(fixture)],
            cwd=self.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        client = TransformTargetClient(self.base_url, timeout=0.2)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise RuntimeError(f"fixture exited early:\n{stdout}\n{stderr}")
            try:
                client.contract()
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("fixture did not become ready")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            if self.process is not None:
                self.process.communicate()
                self.process = None
            return
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)
        self.process = None

    def crash(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate(timeout=5)
        self.process = None


def _declaration(
    root: Path,
    workspace_id: str,
    *,
    delay_seconds: float = 0,
) -> TargetPreflightRequest:
    content = b"independent fixture source\n"
    relative = "camera/source.bin"
    source = workspace_artifact_path(root, "input", workspace_id, relative)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    operation = operation_contract(VIDEO_ARCHIVE_OPERATION)
    intent = VideoArchiveIntent(archive={}).model_dump(mode="json", exclude_none=True)
    return TargetPreflightRequest(
        operation_id=operation.id,
        operation_contract_sha256=operation.contract_sha256,
        workspace_id=workspace_id,
        inputs=(
            Artifact(
                id="source",
                role=SOURCE_ROLE,
                path=relative,
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
        intent=intent,
        target_options={"delay_seconds": delay_seconds},
    )


def _wait_terminal(client: TransformTargetClient, job_id: str) -> TargetJobStatus:
    deadline = time.monotonic() + 10
    status: TargetJobStatus | None = None
    while time.monotonic() < deadline:
        status = client.status(job_id)
        if status.state in {"succeeded", "failed", "canceled"}:
            return status
        time.sleep(0.05)
    raise RuntimeError(f"fixture job did not become terminal: {job_id}: {status}")


def test_independent_fixture_enters_munchy_through_registry_and_custody(
    tmp_path: Path,
) -> None:
    from munchy_core.adapters.transform_targets import (
        HttpTransformTargetPlatform,
        load_target_registry,
    )
    from munchy_core.services import media

    fixture = TargetFixtureProcess(tmp_path / "workspace")
    fixture.root.mkdir()
    fixture.start()
    try:
        client = TransformTargetClient(fixture.base_url)
        target = client.contract()
        raw_registry = json.dumps(
            {
                "external-copy": {
                    "endpoint": fixture.base_url,
                    "workspace_root": str(fixture.root),
                    "expected_protocol": target.protocol,
                    "expected_target_contract_sha256": target.contract_sha256,
                }
            }
        )
        platform = HttpTransformTargetPlatform(load_target_registry(raw_registry))
        media.register_transform_target_platform(platform)
        declaration = _declaration(fixture.root, "fixture-success")
        accepted = platform.preflight("external-copy", declaration)
        request = TargetJobRequest.seal(
            TargetJobRequestPayload(job_id="fixture-success", plan=accepted.plan)
        )

        first = platform.put_job("external-copy", request)
        repeated = platform.put_job("external-copy", request)
        assert repeated.request_sha256 == first.request_sha256 == request.request_sha256
        assert repeated.plan_sha256 == first.plan_sha256 == request.plan.plan_sha256
        succeeded = _wait_terminal(client, request.job_id)
        assert succeeded.state == "succeeded"
        assert succeeded.execution_evidence is not None
        assert succeeded.execution_evidence.target == target
        destination = tmp_path / "accepted"
        outputs = media.accept_target_outputs(
            "external-copy",
            succeeded.model_dump(mode="json", by_alias=True, exclude_none=True),
            destination,
        )
        assert len(outputs) == 1
        assert (destination / outputs[0].path).read_bytes() == b"independent fixture source\n"
        compact = media.compact_target_status_for_progress(
            succeeded.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        assert compact["state"] == "succeeded"
        assert "execution_evidence" not in compact
        assert "outputs" not in compact

        cancel_declaration = _declaration(
            fixture.root,
            "fixture-cancel",
            delay_seconds=2,
        )
        cancel_plan = platform.preflight("external-copy", cancel_declaration).plan
        cancel_request = TargetJobRequest.seal(
            TargetJobRequestPayload(job_id="fixture-cancel", plan=cancel_plan)
        )
        platform.put_job("external-copy", cancel_request)
        platform.cancel(
            "external-copy",
            cancel_request.job_id,
            TargetCancelRequest(reason="fixture cancellation proof"),
        )
        canceled = _wait_terminal(client, cancel_request.job_id)
        assert canceled.state == "canceled"
        assert canceled.failure is not None
        assert canceled.failure.code == "job_canceled"
        assert workspace_artifact_path(
            fixture.root,
            "input",
            cancel_request.job_id,
            "camera/source.bin",
        ).is_file()
        cleanup_roots = media.target_workspace_roots(
            {
                "target_payloads": {
                    "archive_video": {
                        "registration_id": "external-copy",
                        "request": cancel_request.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        ),
                    }
                }
            }
        )
        assert workspace_area_root(fixture.root, "input", cancel_request.job_id) in cleanup_roots
        for root in cleanup_roots:
            shutil.rmtree(root, ignore_errors=True)
        assert not workspace_area_root(fixture.root, "input", cancel_request.job_id).exists()
    finally:
        fixture.stop()


def test_independent_fixture_recovers_interrupted_job_with_same_plan(tmp_path: Path) -> None:
    fixture = TargetFixtureProcess(tmp_path / "workspace")
    fixture.root.mkdir()
    fixture.start()
    client = TransformTargetClient(fixture.base_url)
    try:
        declaration = _declaration(fixture.root, "fixture-restart", delay_seconds=5)
        plan = client.preflight(declaration).plan
        initial = TargetJobRequest.seal(
            TargetJobRequestPayload(job_id="fixture-restart", plan=plan)
        )
        client.put_job(initial)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and client.status(initial.job_id).state != "running":
            time.sleep(0.05)
    finally:
        fixture.crash()

    fixture.start()
    try:
        assert client.status(initial.job_id).state == "interrupted"
        resumed = TargetJobRequest.seal(
            TargetJobRequestPayload(job_id=initial.job_id, attempt=2, plan=initial.plan)
        )
        client.put_job(resumed)
        succeeded = _wait_terminal(client, resumed.job_id)
        assert succeeded.state == "succeeded"
        assert succeeded.attempt == 2
        assert succeeded.plan_sha256 == initial.plan.plan_sha256
    finally:
        fixture.stop()


def test_independent_fixture_has_no_munchy_core_or_hardware_dependency() -> None:
    source = (Path(__file__).parent / "fixtures" / "munchy_transform_target.py").read_text(
        encoding="utf-8"
    )
    assert "munchy_core" not in source
    assert "nvidia" not in source.lower()
