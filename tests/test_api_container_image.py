from __future__ import annotations

import os
import shlex
import time
import uuid

import docker
import httpx
import pytest


def _wait_for_health(base_url: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "service did not become healthy"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"unexpected status {response.status_code}: {response.text}"
        except Exception as exc:  # pragma: no cover - only used on transient startup failures
            last_error = str(exc)
        time.sleep(0.5)
    raise AssertionError(last_error)


def test_api_dockerfile_builds_and_serves_real_container():
    network = os.environ.get("TEST_DOCKER_NETWORK")
    if not network:
        pytest.skip("TEST_DOCKER_NETWORK is not configured")
    image_tag = f"riverhog-api-test:{uuid.uuid4().hex[:12]}"
    container_name = f"riverhog-api-{uuid.uuid4().hex[:12]}"
    api_token = "runtime-api-token"
    base_url = f"http://{container_name}:8080"
    client = docker.from_env()
    container = None

    try:
        client.ping()
        client.images.build(
            path="api",
            dockerfile="Dockerfile",
            tag=image_tag,
            rm=True,
        )
        container = client.containers.run(
            image_tag,
            detach=True,
            remove=True,
            name=container_name,
            network=network,
            environment={
                "API_TOKEN": api_token,
                "API_BASE_URL": base_url,
                "PREFERRED_UID": "1000",
                "PREFERRED_GID": "1000",
            },
        )

        _wait_for_health(base_url)

        unauthorized = httpx.post(
            f"{base_url}/v1/collections",
            json={"description": "blocked"},
            timeout=5.0,
        )
        assert unauthorized.status_code == 401

        authorized = httpx.post(
            f"{base_url}/v1/collections",
            headers={"Authorization": f"Bearer {api_token}"},
            json={
                "root_node_name": "real-image-runtime-check",
                "description": "real image runtime check",
            },
            timeout=5.0,
        )
        assert authorized.status_code == 200, authorized.text
        body = authorized.json()
        assert body["status"] == "open"
        assert body["keep_buffer_after_archive"] is False
        assert body["collection_id"] == "real-image-runtime-check"
        intake_path = body["intake_path"]

        exit_code, output = container.exec_run(
            [
                "sh",
                "-lc",
                (
                    f"mkdir -p {shlex.quote(intake_path)} "
                    f"&& printf 'runtime image seal check\\n' > {shlex.quote(intake_path)}/sample.txt"
                ),
            ],
            user="1000:1000",
        )
        assert exit_code == 0, output.decode()

        sealed = httpx.post(
            f"{base_url}/v1/collections/real-image-runtime-check/seal",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=20.0,
        )
        assert sealed.status_code == 200, sealed.text
        assert sealed.json()["status"] == "sealed"

        exit_code, output = container.exec_run(
            [
                "sh",
                "-lc",
                (
                    "test -d /var/lib/archive/runtime-home "
                    "&& test -d /var/lib/archive/runtime-home/.cache "
                    "&& test -d /var/lib/archive/runtime-home/.config "
                    "&& find /var/lib/archive/runtime-home/.cache -maxdepth 2 -type d | sort"
                ),
            ]
        )
        assert exit_code == 0, output.decode()
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except docker.errors.NotFound:
                pass
        try:
            client.images.remove(image_tag, force=True)
        except docker.errors.ImageNotFound:
            pass
        client.close()
