from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path

from riverhog_api_client.downloads import RetrievalDownload, download_retrieval_files


class DownloadApi:
    def __init__(
        self,
        contents: dict[str, bytes],
        transfer: Callable[[str], None],
    ) -> None:
        self.contents = contents
        self.transfer = transfer

    def spawn(self) -> DownloadApi:
        return DownloadApi(self.contents, self.transfer)

    def download_retrieval_file(
        self,
        job_id: str,
        *,
        collection_id: int,
        path: str,
        output: Path,
        expected_bytes: int,
        expected_sha256: str,
    ) -> int:
        assert job_id == "job-1"
        assert collection_id == 1
        content = self.contents[path]
        assert expected_bytes == len(content)
        assert expected_sha256 == hashlib.sha256(content).hexdigest()
        self.transfer(path)
        output.write_bytes(content)
        return len(content)


def _downloads(tmp_path: Path, contents: dict[str, bytes]) -> list[RetrievalDownload]:
    return [
        RetrievalDownload(
            collection_id=1,
            path=path,
            output=tmp_path / path,
            expected_bytes=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, content in contents.items()
    ]


def test_retrieval_files_download_concurrently_and_preserve_identity(tmp_path: Path) -> None:
    contents = {"one": b"one", "two": b"two"}
    rendezvous = threading.Barrier(2)
    completed: list[str] = []

    def transfer(path: str) -> None:
        rendezvous.wait(timeout=2)
        completed.append(path)

    api = DownloadApi(contents, transfer)
    downloads = _downloads(tmp_path, contents)

    assert (
        download_retrieval_files(
            api,
            "job-1",
            downloads,
            concurrency=2,
            window=2,
        )
        == 6
    )
    assert sorted(completed) == ["one", "two"]
    assert {item.path: item.output.read_bytes() for item in downloads} == contents


def test_download_progress_does_not_block_transfer_workers(tmp_path: Path) -> None:
    contents = {str(index): bytes([index]) for index in range(4)}
    condition = threading.Condition()
    callback_started = threading.Event()
    callback_lock = threading.Lock()
    completed = 0
    callbacks: list[str] = []

    def transfer(path: str) -> None:
        nonlocal completed
        if path != "0":
            assert callback_started.wait(timeout=2)
        with condition:
            completed += 1
            condition.notify_all()

    def downloaded(item: RetrievalDownload, _accepted: int) -> None:
        with callback_lock:
            callbacks.append(item.path)
            if len(callbacks) == 1:
                callback_started.set()
                with condition:
                    assert condition.wait_for(lambda: completed == 4, timeout=2)

    api = DownloadApi(contents, transfer)

    assert (
        download_retrieval_files(
            api,
            "job-1",
            _downloads(tmp_path, contents),
            concurrency=2,
            window=4,
            on_downloaded=downloaded,
        )
        == 4
    )
    assert sorted(callbacks) == ["0", "1", "2", "3"]
