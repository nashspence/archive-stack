from __future__ import annotations

import os
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
    image_tag = f"archive-stack-api-test:{uuid.uuid4().hex[:12]}"
    container_name = f"archive-stack-api-{uuid.uuid4().hex[:12]}"
    api_token = "runtime-api-token"
    hook_secret = "runtime-hook-secret"
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
                "HOOK_SECRET": hook_secret,
                "REDIS_URL": "redis://redis:6379/0",
                "API_BASE_URL": base_url,
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
