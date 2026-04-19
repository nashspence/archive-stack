from __future__ import annotations

import importlib
import os
import socket
import sys
import shutil
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from .helpers import create_collection, force_flush, seal_collection, stage_collection_files
from .mock_data import family_archive_files


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _run_uvicorn(app, *, port: int) -> Iterator[str]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",
        ws="none",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    url = f"http://127.0.0.1:{port}"
    while time.time() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=20)
        raise RuntimeError(f"server on port {port} did not start")

    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def _reset_ui_modules() -> None:
    for module_name in list(sys.modules):
        if module_name == "ui" or module_name.startswith("ui."):
            sys.modules.pop(module_name, None)


def _load_ui_app(*, api_base_url: str, api_token: str):
    previous = {
        "RIVERHOG_API_BASE_URL": os.environ.get("RIVERHOG_API_BASE_URL"),
        "RIVERHOG_API_TOKEN": os.environ.get("RIVERHOG_API_TOKEN"),
    }
    try:
        os.environ["RIVERHOG_API_BASE_URL"] = api_base_url
        os.environ["RIVERHOG_API_TOKEN"] = api_token
        _reset_ui_modules()
        return importlib.import_module("ui.app.main").app
    finally:
        _reset_ui_modules()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _playwright_page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


@contextmanager
def live_ui_stack(module_factory, tmp_path_factory: pytest.TempPathFactory):
    api_port = _free_port()
    ui_port = _free_port()
    api_base_url = f"http://127.0.0.1:{api_port}"

    with module_factory(API_BASE_URL=api_base_url) as loaded:
        ui_app = _load_ui_app(api_base_url=api_base_url, api_token=loaded.env["API_TOKEN"])

        with ExitStack() as stack:
            stack.enter_context(_run_uvicorn(loaded.main.app, port=api_port))
            ui_url = stack.enter_context(_run_uvicorn(ui_app, port=ui_port))
            with httpx.Client(base_url=api_base_url, follow_redirects=True, timeout=30.0) as api_client:
                with _playwright_page() as page:
                    yield {
                        "page": page,
                        "ui_url": ui_url,
                        "loaded": loaded,
                        "api_client": api_client,
                        "tmp_path_factory": tmp_path_factory,
                    }


def _create_collection_via_ui(page: Page, ui_url: str, *, root_node_name: str, description: str) -> None:
    page.goto(ui_url)
    page.get_by_label("Collection name").fill(root_node_name)
    page.get_by_label("Description").fill(description)
    page.get_by_role("button", name="Create collection").click()
    expect(page.get_by_role("heading", name=f"Collection {root_node_name}")).to_be_visible()
    expect(page.get_by_text("Collection created. Populate the intake path, then seal when ready.")).to_be_visible()


def _container_root_path(loaded, container_id: str) -> Path:
    with loaded.db.SessionLocal() as session:
        container = session.get(loaded.models.Container, container_id)
        assert container is not None
        return Path(str(container.root_abs_path))


class _ApiHarness:
    def __init__(self, client: httpx.Client, token: str):
        self.client = client
        self._token = token

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class _StorageHarness:
    def __init__(self, storage):
        self.storage = storage


def test_ui_playwright_dashboard_and_collection_flow(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]

        _create_collection_via_ui(
            page,
            ui_url,
            root_node_name="playwright-home",
            description="Playwright home archive",
        )

        expect(page.locator("code").filter(has_text="playwright-home").first).to_be_visible()

        page.get_by_role("link", name="Dashboard").click()
        expect(page.get_by_role("link", name="playwright-home")).to_be_visible()
        expect(page.get_by_role("heading", name="Partitioning Pool")).to_be_visible()


def test_ui_playwright_collection_seal_and_flush_flow(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]
        loaded = stack["loaded"]
        storage_harness = _StorageHarness(loaded.storage)

        _create_collection_via_ui(
            page,
            ui_url,
            root_node_name="playwright-collection",
            description="Playwright collection archive",
        )

        stage_collection_files(
            storage_harness,
            "playwright-collection",
            [family_archive_files()[0]],
            directories=["docs"],
        )

        page.reload()
        expect(page.get_by_text(family_archive_files()[0].relative_path)).to_be_visible()

        page.get_by_role("button", name="Seal collection").click()
        expect(page.get_by_text("Collection sealed. Closed containers:")).to_be_visible()

        page.get_by_role("link", name="Dashboard").click()
        page.get_by_label("Force close pending containers").check()
        page.get_by_role("button", name="Flush containers").click()
        expect(page.get_by_text("Flush completed. Closed containers: 1.")).to_be_visible(timeout=30_000)

        container_link = page.locator("section").filter(has=page.get_by_role("heading", name="Containers")).get_by_role("link").first
        container_id = (container_link.text_content() or "").strip()
        assert container_id
        container_link.click()
        expect(page.get_by_role("heading", name=f"Container {container_id}")).to_be_visible()
        expect(page.get_by_text("MANIFEST.yml")).to_be_visible()


def test_ui_playwright_container_activation_and_iso_path_flow(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]
        loaded = stack["loaded"]
        api_harness = _ApiHarness(stack["api_client"], loaded.env["API_TOKEN"])
        storage_harness = _StorageHarness(loaded.storage)

        sample = family_archive_files()[0]
        collection_id = create_collection(
            api_harness,
            description="Playwright activation archive",
            root_node_name="playwright-activation",
        )
        stage_collection_files(
            storage_harness,
            collection_id,
            [sample],
        )
        sealed = seal_collection(api_harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(api_harness))[0]
        container_root = _container_root_path(loaded, container_id)

        page.goto(f"{ui_url}/containers/{container_id}")
        page.get_by_role("button", name="Create activation session").click()
        expect(page.get_by_text("Activation session created.")).to_be_visible()
        expect(page.get_by_text("Current activation session:")).to_be_visible()

        staging_path_text = page.locator("code").filter(has_text="/active/activation/staging/").first.text_content()
        assert staging_path_text
        staging_root = Path(staging_path_text)
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        shutil.copytree(container_root, staging_root)

        page.get_by_role("button", name="Complete activation").click()
        expect(page.get_by_text("Activation completed.")).to_be_visible(timeout=60_000)

        iso_bytes = b"PLAYWRIGHT-ISO" * 2048
        iso_path = stack["tmp_path_factory"].mktemp("playwright-iso") / f"{container_id}.iso"
        iso_path.write_bytes(iso_bytes)
        page.get_by_label("Server path").fill(str(iso_path))
        page.get_by_role("button", name="Register existing ISO").click()
        expect(page.get_by_text("ISO registered.")).to_be_visible()
        expect(page.get_by_text(str(iso_path))).to_be_visible()

        page.get_by_role("button", name="Confirm burn").click()
        expect(page.get_by_text("Burn confirmed. Released collections:")).to_be_visible()
