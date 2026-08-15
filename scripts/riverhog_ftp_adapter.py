#!/usr/bin/env python3
"""Minimal FTP-landing to Riverhog collection adapter.

The network-facing FTP daemon is intentionally out of process. It writes into
source-specific FDE-backed landing directories. This adapter owns only stable
file claiming, immutable producer evidence, direct Riverhog collection upload,
and cleanup after a finalized receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from riverhog_api_client import ApiClient
from riverhog_api_client.producer import CollectionProducer, ProducerFile


@dataclass(frozen=True, slots=True)
class SourceConfig:
    id: str
    root: Path
    tags: tuple[str, ...]
    ingest_source: str
    archive_store: str | None
    stable_seconds: int
    max_files: int
    max_bytes: int

    @classmethod
    def from_mapping(cls, source_id: str, value: object) -> SourceConfig:
        if not isinstance(value, dict):
            raise ValueError(f"FTP source {source_id!r} must be an object")
        root = Path(str(value.get("path") or "")).resolve()
        tags = tuple(str(item) for item in value.get("tags") or [])
        if not source_id or not root.is_absolute() or not tags:
            raise ValueError(f"FTP source {source_id!r} is incomplete")
        return cls(
            id=source_id,
            root=root,
            tags=tags,
            ingest_source=str(value.get("ingest_source") or source_id),
            archive_store=(
                str(value["archive_store"]) if value.get("archive_store") is not None else None
            ),
            stable_seconds=max(1, int(value.get("stable_seconds") or 30)),
            max_files=max(1, int(value.get("max_files") or 1000)),
            max_bytes=max(1, int(value.get("max_bytes") or 100 * 1024**3)),
        )


@dataclass(frozen=True, slots=True)
class ClaimedBatch:
    source: SourceConfig
    id: str
    root: Path
    files: tuple[ProducerFile, ...]


class FtpLandingAdapter:
    def __init__(self, api: ApiClient, sources: tuple[SourceConfig, ...]) -> None:
        self.api = api
        self.sources = sources

    def run_once(self) -> int:
        completed = 0
        for source in self.sources:
            source.root.mkdir(parents=True, exist_ok=True)
            for batch in self._existing_batches(source):
                self._publish(batch)
                completed += 1
            batch = self._claim_batch(source)
            if batch is not None:
                self._publish(batch)
                completed += 1
        return completed

    def _claims_root(self, source: SourceConfig) -> Path:
        return source.root / ".riverhog-claims"

    def _existing_batches(self, source: SourceConfig) -> list[ClaimedBatch]:
        root = self._claims_root(source)
        if not root.is_dir():
            return []
        batches: list[ClaimedBatch] = []
        for batch_root in sorted(path for path in root.iterdir() if path.is_dir()):
            manifest_path = batch_root / "batch.json"
            if not manifest_path.is_file():
                continue
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = payload.get("files")
            if not isinstance(rows, list):
                raise RuntimeError(f"claim manifest is invalid: {manifest_path}")
            files = tuple(
                ProducerFile(
                    source=batch_root / "payload" / str(row["path"]),
                    path=str(row["path"]),
                )
                for row in rows
            )
            batches.append(
                ClaimedBatch(source, str(payload["batch_id"]), batch_root, files)
            )
        return batches

    def _eligible(self, source: SourceConfig) -> list[tuple[Path, str, int, int, int]]:
        cutoff_ns = time.time_ns() - source.stable_seconds * 1_000_000_000
        out: list[tuple[Path, str, int, int, int]] = []
        for path in sorted(source.root.rglob("*")):
            if self._claims_root(source) in path.parents:
                continue
            try:
                observed = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_mtime_ns > cutoff_ns:
                continue
            relative = path.relative_to(source.root).as_posix()
            if any(part.startswith(".") for part in Path(relative).parts):
                continue
            out.append(
                (
                    path,
                    relative,
                    observed.st_size,
                    observed.st_dev,
                    observed.st_ino,
                )
            )
        return out

    def _claim_batch(self, source: SourceConfig) -> ClaimedBatch | None:
        selected: list[tuple[Path, str, int, int, int]] = []
        total = 0
        for row in self._eligible(source):
            if selected and (len(selected) >= source.max_files or total + row[2] > source.max_bytes):
                break
            selected.append(row)
            total += row[2]
        if not selected:
            return None
        identity = "\n".join(f"{row[1]}\0{row[2]}\0{row[3]}\0{row[4]}" for row in selected)
        batch_id = hashlib.sha256(
            f"{source.id}\0{identity}".encode("utf-8")
        ).hexdigest()
        batch_root = self._claims_root(source) / batch_id
        payload_root = batch_root / "payload"
        payload_root.mkdir(parents=True, exist_ok=False)
        claimed: list[ProducerFile] = []
        try:
            for original, relative, expected_bytes, device, inode in selected:
                current = os.lstat(original)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_size != expected_bytes
                    or current.st_dev != device
                    or current.st_ino != inode
                ):
                    raise RuntimeError(f"FTP landing file changed before claim: {original}")
                destination = payload_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.rename(original, destination)
                moved = os.lstat(destination)
                if moved.st_dev != device or moved.st_ino != inode:
                    raise RuntimeError(f"FTP landing file changed during claim: {original}")
                claimed.append(ProducerFile(destination, relative))
            manifest = {
                "format": "riverhog-ftp-adapter-batch/v1",
                "batch_id": batch_id,
                "source": source.id,
                "files": [
                    {
                        "path": item.path,
                        "bytes": item.source.stat().st_size,
                    }
                    for item in claimed
                ],
            }
            temporary = batch_root / ".batch.json.part"
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, batch_root / "batch.json")
            return ClaimedBatch(source, batch_id, batch_root, tuple(claimed))
        except Exception:
            # Preserve every claimed byte in the explicit batch directory. A later
            # run can repair/inspect an incomplete manifest; never move bytes back
            # onto a pathname that may now contain a replacement upload.
            raise

    def _publish(self, batch: ClaimedBatch) -> None:
        producer = CollectionProducer(
            self.api,
            producer_app="riverhog-ftp-adapter/v1",
            adapter_id="ftp-landing/v1",
            adapter_version="1.0.0",
            ingest_source=batch.source.ingest_source,
            tags=batch.source.tags,
            archive_store=batch.source.archive_store,
        )
        receipt = producer.publish(
            batch.files,
            source_event_id=f"{batch.source.id}/{batch.id}",
            source_context={"source": batch.source.id, "batch_id": batch.id},
            idempotency_key=batch.id,
            event_context={"adapter": "ftp", "source": batch.source.id},
        )
        result = {
            "collection_id": receipt.collection_id,
            "manifest_sha256": receipt.manifest_sha256,
            "content_etag": receipt.content_etag,
        }
        (batch.root / "receipt.json").write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(batch.root)


def _sources_from_env() -> tuple[SourceConfig, ...]:
    raw = os.environ.get("RIVERHOG_FTP_ADAPTER_SOURCES", "").strip()
    if not raw:
        raise ValueError("RIVERHOG_FTP_ADAPTER_SOURCES is required")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("RIVERHOG_FTP_ADAPTER_SOURCES must be a nonempty object")
    return tuple(
        SourceConfig.from_mapping(str(source_id), value)
        for source_id, value in sorted(payload.items())
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Commit stable FTP landing files directly to finalized Riverhog collections."
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    with ApiClient() as api:
        adapter = FtpLandingAdapter(api, _sources_from_env())
        while True:
            adapter.run_once()
            if args.once:
                return 0
            time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
