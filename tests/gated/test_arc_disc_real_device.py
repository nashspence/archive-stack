from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MethodType

import pytest

from arc_core.domain.errors import InvalidState, NotFound
from arc_core.iso.streaming import build_iso_cmd_from_root
from arc_disc import main as arc_disc_main
from tests.fixtures.acceptance import AcceptanceSystem
from tests.fixtures.data import DOCS_COLLECTION_ID, IMAGE_ID, INVOICE_TARGET

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
_FAKE_FACTORY_ENV_VARS = (
    "ARC_DISC_READER_FACTORY",
    "ARC_DISC_ISO_VERIFIER_FACTORY",
    "ARC_DISC_BURNER_FACTORY",
    "ARC_DISC_BURNED_MEDIA_VERIFIER_FACTORY",
    "ARC_DISC_BURN_PROMPTS_FACTORY",
)
_BURN_CONFIRMATION = "write-optical-media"


def _reject_fake_factories() -> None:
    configured = [name for name in _FAKE_FACTORY_ENV_VARS if os.environ.get(name)]
    if configured:
        names = ", ".join(configured)
        pytest.fail(f"gated arc-disc validation must not use fake factory env vars: {names}")


def _required_env(name: str, *, reason: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    pytest.skip(f"{name} is required for {reason}")


def _optional_env(primary: str, fallback: str, *, reason: str) -> str:
    value = os.environ.get(primary) or os.environ.get(fallback)
    if value:
        return value
    pytest.skip(f"{primary} or {fallback} is required for {reason}")


def _required_path(name: str, *, reason: str) -> Path:
    path = Path(_required_env(name, reason=reason)).expanduser()
    if path.exists():
        return path
    pytest.skip(f"{name} does not exist: {path}")


def _require_xorriso(*, reason: str) -> None:
    if shutil.which("xorriso") is None:
        pytest.skip(f"xorriso is required for {reason}")


def _require_destructive_opt_in(*, reason: str) -> None:
    if os.environ.get("ARC_DISC_GATED_BURN_CONFIRM") == _BURN_CONFIRMATION:
        return
    pytest.skip(
        "set ARC_DISC_GATED_BURN_CONFIRM=write-optical-media to run destructive "
        f"{reason}"
    )


def _required_burn_device(*, reason: str) -> Path:
    device = _required_path("ARC_DISC_GATED_BURN_DEVICE", reason=reason)
    if not os.access(device, os.R_OK | os.W_OK):
        pytest.skip(f"ARC_DISC_GATED_BURN_DEVICE must be readable and writable: {device}")
    return device


def _run_arc_disc(
    acceptance_system: AcceptanceSystem,
    *args: str,
    input_text: str = "\n" * 32,
    staging_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(SRC_ROOT), str(REPO_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["ARC_BASE_URL"] = acceptance_system.base_url
    if staging_dir is not None:
        env["ARC_DISC_STAGING_DIR"] = str(staging_dir)
    return subprocess.run(
        [sys.executable, "-m", "arc_disc.main", *args],
        cwd=REPO_ROOT,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def gated_acceptance_system(tmp_path: Path) -> Iterator[AcceptanceSystem]:
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    try:
        yield system
    finally:
        system.close()


def _assert_arc_disc_succeeded(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        "arc-disc command failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _response_json(response: object) -> Mapping[str, object]:
    payload = response.json()  # type: ignore[attr-defined]
    if not isinstance(payload, Mapping):
        raise AssertionError(f"expected JSON object response, got {type(payload).__name__}")
    return payload


def _real_iso_bytes(image: object) -> bytes:
    cmd = build_iso_cmd_from_root(
        image_root=image.image_root,  # type: ignore[attr-defined]
        volume_id=str(image.finalized_id),  # type: ignore[attr-defined]
    )
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode == 0:
        return proc.stdout
    detail = proc.stderr.decode("utf-8", errors="replace")[-1500:] or (
        f"xorriso exited {proc.returncode}"
    )
    raise RuntimeError(f"gated fake API could not build a real ISO: {detail}")


def _configure_fake_api_real_iso_streams(acceptance_system: AcceptanceSystem) -> None:
    def _fixture_iso_bytes(_planning: object, image: object) -> bytes:
        return _real_iso_bytes(image)

    def _iter_restored_iso(
        recovery_sessions: object,
        session_id: str,
        image_id: str,
    ) -> Iterator[bytes]:
        with acceptance_system.state.lock:
            record = acceptance_system.state.recovery_sessions_by_id.get(session_id)
            if record is None:
                raise NotFound(f"recovery session not found: {session_id}")
            if record.state.value != "ready":
                raise InvalidState("recovery session is not ready for ISO download")
            if str(record.image_id) != image_id:
                raise NotFound(f"image not found in recovery session: {image_id}")
            image = acceptance_system.state.finalized_images_by_id[record.image_id]
        yield _real_iso_bytes(image)

    acceptance_system.planning._fixture_iso_bytes = MethodType(  # type: ignore[method-assign]
        _fixture_iso_bytes,
        acceptance_system.planning,
    )
    acceptance_system.recovery_sessions.iter_restored_iso = MethodType(  # type: ignore[method-assign]
        _iter_restored_iso,
        acceptance_system.recovery_sessions,
    )


def _register_existing_copy(
    acceptance_system: AcceptanceSystem,
    *,
    image_id: str,
    copy_id: str,
) -> None:
    response = acceptance_system.request(
        "POST",
        f"/v1/images/{image_id}/copies",
        json_body={"copy_id": copy_id, "location": "fake-backed existing shelf"},
    )
    assert response.status_code == 200, response.text
    response = acceptance_system.request(
        "PATCH",
        f"/v1/images/{image_id}/copies/{copy_id}",
        json_body={"state": "verified", "verification_state": "verified"},
    )
    assert response.status_code == 200, response.text


def _mark_copy_lost(
    acceptance_system: AcceptanceSystem,
    *,
    image_id: str,
    copy_id: str,
) -> None:
    response = acceptance_system.request(
        "PATCH",
        f"/v1/images/{image_id}/copies/{copy_id}",
        json_body={"state": "lost"},
    )
    assert response.status_code == 200, response.text


def _mark_copy_damaged(
    acceptance_system: AcceptanceSystem,
    *,
    image_id: str,
    copy_id: str,
) -> None:
    response = acceptance_system.request(
        "PATCH",
        f"/v1/images/{image_id}/copies/{copy_id}",
        json_body={"state": "damaged"},
    )
    assert response.status_code == 200, response.text


def _seed_one_real_burn_needed(acceptance_system: AcceptanceSystem) -> str:
    acceptance_system.seed_planner_fixtures()
    response = acceptance_system.request("POST", f"/v1/plan/candidates/{IMAGE_ID}/finalize")
    assert response.status_code == 200, response.text
    image_id = str(_response_json(response)["id"])
    _register_existing_copy(
        acceptance_system,
        image_id=image_id,
        copy_id=f"{image_id}-1",
    )
    return image_id


def _make_finalized_target_fetchable_from_only_copy(
    acceptance_system: AcceptanceSystem,
    *,
    image_id: str,
    kept_copy_id: str,
) -> str:
    _mark_copy_lost(
        acceptance_system,
        image_id=image_id,
        copy_id=f"{image_id}-1",
    )
    acceptance_system.constrain_collection_to_finalized_image_coverage(
        DOCS_COLLECTION_ID,
        image_id,
        hot=False,
        archived=True,
    )
    response = acceptance_system.request("POST", "/v1/pin", json_body={"target": INVOICE_TARGET})
    assert response.status_code == 200, response.text
    payload = _response_json(response)
    fetch = payload["fetch"]
    assert isinstance(fetch, Mapping)
    fetch_id = str(fetch["id"])
    manifest = acceptance_system.fetches.manifest(fetch_id)
    copies = [
        copy
        for entry in manifest["entries"]  # type: ignore[index]
        for part in entry["parts"]
        for copy in part["copies"]
    ]
    assert {copy["copy"] for copy in copies} == {kept_copy_id}
    return fetch_id


def _prepare_recovery_session_with_one_real_burn_needed(
    acceptance_system: AcceptanceSystem,
) -> tuple[str, str, str]:
    acceptance_system.seed_planner_fixtures()
    response = acceptance_system.request("POST", f"/v1/plan/candidates/{IMAGE_ID}/finalize")
    assert response.status_code == 200, response.text
    image_id = str(_response_json(response)["id"])
    acceptance_system.wait_for_image_glacier_state(image_id, "uploaded")
    for copy_id, location in (
        (f"{image_id}-1", "fake-backed recovery shelf a"),
        (f"{image_id}-2", "fake-backed recovery shelf b"),
    ):
        response = acceptance_system.request(
            "POST",
            f"/v1/images/{image_id}/copies",
            json_body={"copy_id": copy_id, "location": location},
        )
        assert response.status_code == 200, response.text
    _mark_copy_lost(acceptance_system, image_id=image_id, copy_id=f"{image_id}-1")
    _mark_copy_damaged(acceptance_system, image_id=image_id, copy_id=f"{image_id}-2")
    session = acceptance_system.recovery_sessions.get_for_image(image_id)
    response = acceptance_system.request("POST", f"/v1/recovery-sessions/{session.id}/approve")
    assert response.status_code == 200, response.text
    acceptance_system.wait_for_recovery_session_state(str(session.id), "ready")
    _register_existing_copy(
        acceptance_system,
        image_id=image_id,
        copy_id=f"{image_id}-3",
    )
    return image_id, str(session.id), f"{image_id}-4"


def _sha256_chunks(chunks: Iterator[bytes]) -> str:
    digest = hashlib.sha256()
    saw_bytes = False
    for chunk in chunks:
        if not chunk:
            continue
        saw_bytes = True
        digest.update(chunk)
    if not saw_bytes:
        raise AssertionError("arc-disc optical reader returned no bytes")
    return digest.hexdigest()


def _read_recovery_copy_sha256(
    reader: object,
    *,
    disc_path: str,
    device: str,
) -> str:
    copy = arc_disc_main.RecoveryCopyHint(
        copy_id="gated-arc-disc-copy",
        location="gated validation media",
        disc_path=disc_path,
        recovery_bytes=0,
        recovery_sha256="",
    )
    return _sha256_chunks(arc_disc_main._iter_recovered_chunks(reader, copy, device=device))


def test_default_reader_recovers_configured_mounted_media_payload() -> None:
    _reject_fake_factories()
    mount_path = _required_path(
        "ARC_DISC_GATED_MOUNT_PATH",
        reason="mounted optical-media recovery validation",
    )
    if not mount_path.is_dir():
        pytest.skip(f"ARC_DISC_GATED_MOUNT_PATH must be a mounted directory: {mount_path}")
    disc_path = _required_env(
        "ARC_DISC_GATED_PAYLOAD_PATH",
        reason="mounted optical-media recovery validation",
    )
    expected_sha256 = _required_env(
        "ARC_DISC_GATED_EXPECTED_SHA256",
        reason="mounted optical-media recovery validation",
    )

    reader = arc_disc_main.build_optical_reader()
    if not isinstance(reader, arc_disc_main.XorrisoOpticalReader):
        raise AssertionError(f"default reader is not the real backend: {type(reader).__name__}")

    actual_sha256 = _read_recovery_copy_sha256(
        reader,
        disc_path=disc_path,
        device=str(mount_path),
    )

    assert actual_sha256 == expected_sha256


def test_default_reader_recovers_configured_raw_device_payload() -> None:
    _reject_fake_factories()
    _require_xorriso(reason="raw optical-device recovery validation")
    device = _required_path(
        "ARC_DISC_GATED_RAW_DEVICE",
        reason="raw optical-device recovery validation",
    )
    if not os.access(device, os.R_OK):
        pytest.skip(f"ARC_DISC_GATED_RAW_DEVICE is not readable by this user: {device}")
    disc_path = _optional_env(
        "ARC_DISC_GATED_RAW_PAYLOAD_PATH",
        "ARC_DISC_GATED_PAYLOAD_PATH",
        reason="raw optical-device recovery validation",
    )
    expected_sha256 = _optional_env(
        "ARC_DISC_GATED_RAW_EXPECTED_SHA256",
        "ARC_DISC_GATED_EXPECTED_SHA256",
        reason="raw optical-device recovery validation",
    )

    reader = arc_disc_main.build_optical_reader()
    if not isinstance(reader, arc_disc_main.XorrisoOpticalReader):
        raise AssertionError(f"default reader is not the real backend: {type(reader).__name__}")

    actual_sha256 = _read_recovery_copy_sha256(
        reader,
        disc_path=disc_path,
        device=str(device),
    )

    assert actual_sha256 == expected_sha256


def test_destructive_burn_backend_writes_and_verifies_configured_media() -> None:
    _reject_fake_factories()
    _require_xorriso(reason="destructive optical burn validation")
    _require_destructive_opt_in(reason="optical burn validation")
    device = _required_path(
        "ARC_DISC_GATED_BURN_DEVICE",
        reason="destructive optical burn validation",
    )
    iso_path = _required_path(
        "ARC_DISC_GATED_BURN_ISO_PATH",
        reason="destructive optical burn validation",
    )
    if not iso_path.is_file():
        pytest.skip(f"ARC_DISC_GATED_BURN_ISO_PATH must be an ISO file: {iso_path}")
    if not os.access(device, os.R_OK | os.W_OK):
        pytest.skip(f"ARC_DISC_GATED_BURN_DEVICE must be readable and writable: {device}")
    copy_id = os.environ.get("ARC_DISC_GATED_BURN_COPY_ID", "gated-arc-disc-copy")

    burner = arc_disc_main.build_disc_burner()
    if not isinstance(burner, arc_disc_main.XorrisoDiscBurner):
        raise AssertionError(f"default burner is not the real backend: {type(burner).__name__}")
    verifier = arc_disc_main.build_burned_media_verifier()
    if not isinstance(verifier, arc_disc_main.RawBurnedMediaVerifier):
        raise AssertionError(
            f"default burned-media verifier is not the real backend: {type(verifier).__name__}"
        )

    burner.burn(iso_path, device=str(device), copy_id=copy_id)
    verifier.verify(iso_path, device=str(device), copy_id=copy_id)


def test_full_burn_then_fetch_cli_workflow_uses_fake_api_and_real_optical_device(
    gated_acceptance_system: AcceptanceSystem,
    tmp_path: Path,
) -> None:
    _reject_fake_factories()
    _require_xorriso(reason="full arc-disc burn/fetch CLI workflow validation")
    _require_destructive_opt_in(reason="full arc-disc burn/fetch CLI workflow validation")
    device = _required_burn_device(reason="full arc-disc burn/fetch CLI workflow validation")
    _configure_fake_api_real_iso_streams(gated_acceptance_system)
    image_id = _seed_one_real_burn_needed(gated_acceptance_system)
    burned_copy_id = f"{image_id}-2"
    staging_dir = tmp_path / "arc-disc-burn-fetch-staging"

    result = _run_arc_disc(
        gated_acceptance_system,
        "burn",
        "--device",
        str(device),
        "--staging-dir",
        str(staging_dir),
        input_text="\nlabeled\ngated real optical shelf\n",
        staging_dir=staging_dir,
    )

    _assert_arc_disc_succeeded(result)
    assert "burn backlog cleared" in result.stdout
    assert burned_copy_id in result.stdout

    fetch_id = _make_finalized_target_fetchable_from_only_copy(
        gated_acceptance_system,
        image_id=image_id,
        kept_copy_id=burned_copy_id,
    )
    result = _run_arc_disc(
        gated_acceptance_system,
        "fetch",
        fetch_id,
        "--device",
        str(device),
        "--json",
        input_text="\n",
    )

    _assert_arc_disc_succeeded(result)
    payload = json.loads(result.stdout)
    assert payload["state"] == "done"


def test_full_recover_cli_workflow_uses_fake_api_restored_iso_and_real_optical_device(
    gated_acceptance_system: AcceptanceSystem,
    tmp_path: Path,
) -> None:
    _reject_fake_factories()
    _require_xorriso(reason="full arc-disc recover CLI workflow validation")
    _require_destructive_opt_in(reason="full arc-disc recover CLI workflow validation")
    device = _required_burn_device(reason="full arc-disc recover CLI workflow validation")
    _configure_fake_api_real_iso_streams(gated_acceptance_system)
    _image_id, session_id, burned_copy_id = _prepare_recovery_session_with_one_real_burn_needed(
        gated_acceptance_system
    )
    staging_dir = tmp_path / "arc-disc-recover-staging"

    result = _run_arc_disc(
        gated_acceptance_system,
        "recover",
        session_id,
        "--device",
        str(device),
        "--staging-dir",
        str(staging_dir),
        input_text="\nlabeled\ngated recovered optical shelf\n",
        staging_dir=staging_dir,
    )

    _assert_arc_disc_succeeded(result)
    assert f"recovery session {session_id} completed" in result.stdout
    assert burned_copy_id in result.stdout
