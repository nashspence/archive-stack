from __future__ import annotations

import base64
import importlib
import os
import socket
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from playwright.sync_api import Page, expect, sync_playwright

from .helpers import create_collection, force_flush, seal_collection, upload_collection_file
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


def _decode_tus_metadata(header: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if not header.strip():
        return metadata
    for item in header.split(","):
        key, encoded = item.strip().split(" ", 1)
        metadata[key] = base64.b64decode(encoded).decode("utf-8")
    return metadata


def _build_fake_tusd(*, api_base_url: str, hook_secret: str) -> FastAPI:
    app = FastAPI()
    uploads: dict[str, dict[str, object]] = {}

    def _hook(hook_name: str, payload: dict[str, object]) -> dict[str, object]:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                f"{api_base_url}/internal/tusd-hooks?hook_secret={hook_secret}",
                headers={"Hook-Name": hook_name},
                json=payload,
            )
        response.raise_for_status()
        return response.json()

    @app.post("/files")
    async def create_upload(
        upload_length: int = Header(alias="Upload-Length"),
        upload_metadata: str = Header(default="", alias="Upload-Metadata"),
    ):
        metadata = _decode_tus_metadata(upload_metadata)
        upload_id = metadata["upload_id"]
        precreate = _hook(
            "pre-create",
            {
                "ID": upload_id,
                "Size": int(upload_length),
                "MetaData": metadata,
            },
        )
        incoming_path = Path(precreate["ChangeFileInfo"]["Storage"]["Path"])
        incoming_path.parent.mkdir(parents=True, exist_ok=True)
        incoming_path.write_bytes(b"")
        uploads[upload_id] = {
            "path": incoming_path,
            "size": int(upload_length),
            "offset": 0,
        }
        _hook("post-create", {"ID": upload_id})
        return Response(
            status_code=201,
            headers={
                "Location": f"/files/{upload_id}",
                "Tus-Resumable": "1.0.0",
            },
        )

    @app.patch("/files/{upload_id}")
    async def patch_upload(
        upload_id: str,
        request: Request,
        upload_offset: int = Header(alias="Upload-Offset"),
    ):
        upload = uploads.get(upload_id)
        if upload is None:
            raise HTTPException(status_code=404, detail="unknown upload")
        if int(upload_offset) != int(upload["offset"]):
            raise HTTPException(status_code=409, detail="unexpected upload offset")

        content = await request.body()
        path = Path(str(upload["path"]))
        with path.open("ab") as handle:
            handle.write(content)
        upload["offset"] = int(upload["offset"]) + len(content)

        _hook("post-receive", {"ID": upload_id, "Offset": int(upload["offset"])})
        if int(upload["offset"]) >= int(upload["size"]):
            _hook("post-finish", {"ID": upload_id})

        return Response(
            status_code=204,
            headers={
                "Upload-Offset": str(upload["offset"]),
                "Tus-Resumable": "1.0.0",
            },
        )

    return app


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


@dataclass
class RemoteHarness:
    client: httpx.Client
    loaded: Any

    @property
    def archive_root(self) -> Path:
        return self.loaded.archive_root

    @property
    def models(self) -> Any:
        return self.loaded.models

    @property
    def hook_secret(self) -> str:
        return str(self.loaded.env["HOOK_SECRET"])

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.loaded.env['API_TOKEN']}"}

    def hook_headers(self, hook_name: str) -> dict[str, str]:
        return {"Hook-Name": hook_name}

    def hook_url(self) -> str:
        return f"/internal/tusd-hooks?hook_secret={self.hook_secret}"

    @contextmanager
    def session(self):
        session = self.loaded.db.SessionLocal()
        try:
            yield session
        finally:
            session.close()


@contextmanager
def live_ui_stack(module_factory, tmp_path_factory: pytest.TempPathFactory):
    api_port = _free_port()
    tusd_port = _free_port()
    ui_port = _free_port()
    api_base_url = f"http://127.0.0.1:{api_port}"
    tusd_base_url = f"http://127.0.0.1:{tusd_port}/files"

    with module_factory(API_BASE_URL=api_base_url, TUSD_BASE_URL=tusd_base_url) as loaded:
        ui_app = _load_ui_app(api_base_url=api_base_url, api_token=loaded.env["API_TOKEN"])
        fake_tusd = _build_fake_tusd(api_base_url=api_base_url, hook_secret=loaded.env["HOOK_SECRET"])

        with ExitStack() as stack:
            stack.enter_context(_run_uvicorn(loaded.main.app, port=api_port))
            stack.enter_context(_run_uvicorn(fake_tusd, port=tusd_port))
            ui_url = stack.enter_context(_run_uvicorn(ui_app, port=ui_port))
            with httpx.Client(base_url=api_base_url, follow_redirects=True, timeout=30.0) as api_client:
                harness = RemoteHarness(client=api_client, loaded=loaded)
                with _playwright_page() as page:
                    yield {
                        "page": page,
                        "ui_url": ui_url,
                        "loaded": loaded,
                        "harness": harness,
                        "tmp_path_factory": tmp_path_factory,
                    }


def _create_collection_via_ui(page: Page, ui_url: str, *, root_node_name: str, description: str) -> None:
    page.goto(ui_url)
    page.get_by_label("Root node name").fill(root_node_name)
    page.get_by_label("Description").fill(description)
    page.get_by_role("button", name="Create collection").click()
    expect(page.get_by_role("heading", name=f"Collection {root_node_name}")).to_be_visible()
    expect(page.get_by_text("Collection created.")).to_be_visible()


def _container_root_path(loaded, container_id: str) -> Path:
    with loaded.db.SessionLocal() as session:
        container = session.get(loaded.models.Container, container_id)
        assert container is not None
        return Path(str(container.root_abs_path))


def test_ui_playwright_dashboard_collection_and_webhook_flow(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]

        _create_collection_via_ui(
            page,
            ui_url,
            root_node_name="playwright-home",
            description="Playwright home archive",
        )

        page.get_by_role("link", name="Dashboard").click()
        expect(page.get_by_role("link", name="playwright-home")).to_be_visible()

        page.get_by_label("Webhook URL").fill("https://example.test/riverhog-hook")
        page.get_by_role("button", name="Create finalization webhook").click()
        expect(page.get_by_text("Webhook created. Pending containers: 0.")).to_be_visible()


def test_ui_playwright_collection_upload_and_flush_flow(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]

        _create_collection_via_ui(
            page,
            ui_url,
            root_node_name="playwright-collection",
            description="Playwright collection archive",
        )

        page.get_by_label("Relative directory path").fill("docs")
        page.get_by_role("button", name="Create directory").click()
        expect(page.get_by_text("Directory created.")).to_be_visible()

        upload_file = stack["tmp_path_factory"].mktemp("playwright-upload") / "notes.txt"
        upload_file.write_text("riverhog ui upload test\n", encoding="utf-8")

        page.get_by_label("Path prefix").fill("docs")
        page.locator("#collection-files").set_input_files(str(upload_file))
        page.evaluate(
            """
            () => {
              document
                .getElementById("collection-upload-form")
                ?.removeAttribute("data-progress-url");
            }
            """
        )
        with page.expect_navigation(wait_until="load", timeout=30_000):
            page.get_by_role("button", name="Upload selected files").click()
        expect(page.get_by_text("docs/notes.txt")).to_be_visible(timeout=30_000)

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


def test_ui_playwright_collection_uploads_run_in_parallel(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]

        _create_collection_via_ui(
            page,
            ui_url,
            root_node_name="playwright-parallel",
            description="Playwright parallel uploads",
        )

        upload_dir = stack["tmp_path_factory"].mktemp("playwright-parallel-files")
        upload_paths: list[str] = []
        for index in range(4):
            path = upload_dir / f"file-{index}.txt"
            path.write_text(f"parallel upload {index}\n", encoding="utf-8")
            upload_paths.append(str(path))

        page.evaluate(
            """
            () => {
              localStorage.setItem(
                "uploadStats",
                JSON.stringify({ active: 0, calls: 0, max: 0 })
              );
              const readStats = () => JSON.parse(localStorage.getItem("uploadStats") || "{}");
              const writeStats = (stats) => localStorage.setItem("uploadStats", JSON.stringify(stats));

              window.fetch = async () => {
                const started = readStats();
                started.calls += 1;
                started.active += 1;
                started.max = Math.max(started.max, started.active);
                writeStats(started);

                await new Promise((resolve) => setTimeout(resolve, 200));

                const finished = readStats();
                finished.active -= 1;
                writeStats(finished);

                return new Response(JSON.stringify({ status: "ok" }), {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                });
              };

              document
                .getElementById("collection-upload-form")
                ?.removeAttribute("data-progress-url");
            }
            """
        )
        page.locator("#collection-files").set_input_files(upload_paths)

        with page.expect_navigation(wait_until="load", timeout=30_000):
            page.get_by_role("button", name="Upload selected files").click()

        page.wait_for_function(
            """
            () => {
              const stats = JSON.parse(localStorage.getItem("uploadStats") || "{}");
              return stats.calls === 4 && stats.active === 0;
            }
            """,
            timeout=30_000,
        )
        stats = page.evaluate("() => JSON.parse(localStorage.getItem('uploadStats') || '{}')")
        assert stats["calls"] == 4
        assert stats["max"] > 1


def test_ui_playwright_collection_retry_skips_already_uploaded_files(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]

        _create_collection_via_ui(
            page,
            ui_url,
            root_node_name="playwright-retry",
            description="Playwright retry skips uploaded files",
        )

        upload_dir = stack["tmp_path_factory"].mktemp("playwright-retry-files")
        first_file = upload_dir / "first.txt"
        second_file = upload_dir / "second.txt"
        first_file.write_text("already uploaded\n", encoding="utf-8")
        second_file.write_text("new file on retry\n", encoding="utf-8")

        page.evaluate(
            """
            () => {
              localStorage.setItem("retryStats", JSON.stringify([]));
              const record = (entry) => {
                const stats = JSON.parse(localStorage.getItem("retryStats") || "[]");
                stats.push(entry);
                localStorage.setItem("retryStats", JSON.stringify(stats));
              };

              window.fetch = async (_url, options) => {
                const relativePath = options?.body?.get("relative_path");
                record(relativePath);
                if (relativePath === "first.txt") {
                  return new Response(
                    JSON.stringify({ detail: "file already uploaded for this collection" }),
                    {
                      status: 409,
                      headers: { "Content-Type": "application/json" },
                    }
                  );
                }
                return new Response(JSON.stringify({ status: "ok" }), {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                });
              };

              document
                .getElementById("collection-upload-form")
                ?.removeAttribute("data-progress-url");
            }
            """
        )
        page.locator("#collection-files").set_input_files([str(first_file), str(second_file)])
        with page.expect_navigation(wait_until="load", timeout=30_000):
            page.get_by_role("button", name="Upload selected files").click()
        stats = page.evaluate("() => JSON.parse(localStorage.getItem('retryStats') || '[]')")
        assert sorted(stats) == ["first.txt", "second.txt"]


def test_ui_playwright_container_activation_and_download_flow(module_factory, tmp_path_factory):
    with live_ui_stack(module_factory, tmp_path_factory) as stack:
        page = stack["page"]
        ui_url = stack["ui_url"]
        loaded = stack["loaded"]
        harness = stack["harness"]

        sample = family_archive_files()[0]
        collection_id = create_collection(
            harness,
            description="Playwright activation archive",
            root_node_name="playwright-activation",
        )
        upload_collection_file(harness, collection_id, sample)
        sealed = seal_collection(harness, collection_id)
        container_id = (sealed["closed_containers"] or force_flush(harness))[0]
        container_root = _container_root_path(loaded, container_id)

        page.goto(f"{ui_url}/containers/{container_id}")
        page.get_by_role("button", name="Create activation session").click()
        expect(page.get_by_text("Activation session created.")).to_be_visible()
        expect(page.get_by_text("Current activation session:")).to_be_visible()

        page.locator("#activation-folder").set_input_files(str(container_root))
        page.evaluate(
            """
            () => {
              document
                .getElementById("activation-upload-form")
                ?.removeAttribute("data-progress-url");
            }
            """
        )
        with page.expect_navigation(wait_until="load", timeout=60_000):
            page.get_by_role("button", name="Upload activation root and complete").click()
        expect(page.get_by_text("Activation completed.")).to_be_visible(timeout=60_000)

        iso_bytes = b"PLAYWRIGHT-ISO" * 2048
        iso_path = stack["tmp_path_factory"].mktemp("playwright-iso") / f"{container_id}.iso"
        iso_path.write_bytes(iso_bytes)
        page.get_by_label("Server path").fill(str(iso_path))
        page.get_by_role("button", name="Register existing ISO").click()
        expect(page.get_by_text("ISO registered.")).to_be_visible()

        page.get_by_role("button", name="Create download session").click()
        expect(page.get_by_text("Download session created.")).to_be_visible()

        with page.expect_download() as download_info:
            page.get_by_role("link", name="Download via session").click()
        download = download_info.value
        saved_path = stack["tmp_path_factory"].mktemp("playwright-download") / "session.iso"
        download.save_as(str(saved_path))
        assert saved_path.read_bytes() == iso_bytes

        page.get_by_role("button", name="Confirm burn").click()
        expect(page.get_by_text("Burn confirmed. Released collections:")).to_be_visible()
