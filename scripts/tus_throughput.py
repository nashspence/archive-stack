#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from urllib.parse import urljoin

import httpx

from riverhog_core.tus_upload import DEFAULT_TUS_UPLOAD_CHUNK_MIB

TUS_VERSION = "1.0.0"
DEFAULT_CHUNK_MIB = (DEFAULT_TUS_UPLOAD_CHUNK_MIB, 128)
USER_ENV = "TUS_BENCHMARK_USER"
PASSWORD_ENV = "TUS_BENCHMARK_PASSWORD"


@dataclass(frozen=True)
class ThroughputResult:
    chunk_mib: int
    gbit_per_second: float
    http_version: str
    mib_per_second: float
    seconds: float
    size_mib: int


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _credentials_from_env() -> tuple[str, str] | None:
    user = os.getenv(USER_ENV, "")
    password = os.getenv(PASSWORD_ENV, "")
    if bool(user) != bool(password):
        raise ValueError(f"{USER_ENV} and {PASSWORD_ENV} must be set together")
    return (user, password) if user else None


def _require_termination(client: httpx.Client, base_url: str) -> None:
    response = client.options(base_url, headers={"Tus-Resumable": TUS_VERSION})
    response.raise_for_status()
    extensions = {
        extension.strip()
        for extension in response.headers.get("Tus-Extension", "").split(",")
        if extension.strip()
    }
    if "termination" not in extensions:
        raise RuntimeError(
            "the TUS endpoint must support termination so the incomplete probe can be deleted"
        )


def _measure_probe(
    client: httpx.Client,
    base_url: str,
    *,
    size_mib: int,
    chunk_mib: int,
    clock: Callable[[], float] = time.perf_counter,
) -> ThroughputResult:
    total_bytes = size_mib * 1024 * 1024
    chunk_bytes = chunk_mib * 1024 * 1024
    probe_path = f"tus-throughput-{time.time_ns()}-{chunk_mib}m.bin"
    encoded_path = base64.b64encode(probe_path.encode()).decode()
    upload_url: str | None = None

    try:
        response = client.post(
            base_url,
            headers={
                "Tus-Resumable": TUS_VERSION,
                # Keep the probe incomplete so post-finish publication never runs.
                "Upload-Length": str(total_bytes + 1),
                "Upload-Metadata": f"path {encoded_path}",
            },
        )
        response.raise_for_status()
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("the TUS create response omitted Location")
        upload_url = urljoin(base_url, location)

        offset = 0
        payload = bytes(chunk_bytes)
        http_version = response.http_version
        started = clock()
        while offset < total_bytes:
            length = min(chunk_bytes, total_bytes - offset)
            response = client.patch(
                upload_url,
                headers={
                    "Tus-Resumable": TUS_VERSION,
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                },
                content=payload if length == chunk_bytes else payload[:length],
            )
            response.raise_for_status()
            http_version = response.http_version
            expected_offset = offset + length
            actual_offset = int(response.headers.get("Upload-Offset", "-1"))
            if actual_offset != expected_offset:
                raise RuntimeError(
                    f"unexpected TUS offset {actual_offset}; expected {expected_offset}"
                )
            offset = actual_offset
        elapsed = clock() - started

        return ThroughputResult(
            chunk_mib=chunk_mib,
            gbit_per_second=round(total_bytes * 8 / elapsed / 1_000_000_000, 3),
            http_version=http_version,
            mib_per_second=round(size_mib / elapsed, 2),
            seconds=round(elapsed, 3),
            size_mib=size_mib,
        )
    finally:
        if upload_url is not None:
            cleanup = client.delete(
                upload_url,
                headers={"Tus-Resumable": TUS_VERSION},
            )
            cleanup.raise_for_status()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure a TUS endpoint without completing or publishing the synthetic upload. "
            "Results are emitted as JSON lines."
        )
    )
    parser.add_argument("url", help="TUS creation URL")
    parser.add_argument(
        "--size-mib",
        type=_positive_int,
        default=1024,
        help="payload size per measurement (default: 1024)",
    )
    parser.add_argument(
        "--chunk-mib",
        action="append",
        type=_positive_int,
        help="PATCH size; repeat for a matrix (default: 64 and 128)",
    )
    parser.add_argument(
        "--http-version",
        choices=("1.1", "2"),
        default="2",
        help="client HTTP version preference (default: 2)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_int,
        default=300,
        help="per-request timeout (default: 300)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    chunk_sizes = args.chunk_mib or list(DEFAULT_CHUNK_MIB)
    auth = _credentials_from_env()
    with httpx.Client(
        auth=auth,
        http2=args.http_version == "2",
        timeout=float(args.timeout_seconds),
        headers={"User-Agent": "tus-throughput/1"},
    ) as client:
        _require_termination(client, args.url)
        for chunk_mib in chunk_sizes:
            result = _measure_probe(
                client,
                args.url,
                size_mib=args.size_mib,
                chunk_mib=chunk_mib,
            )
            print(json.dumps(asdict(result), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
