#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from urllib.parse import urljoin

import httpx
from tus_transport import DEFAULT_TUS_UPLOAD_CHUNK_MIB, TusTransport

TUS_VERSION = "1.0.0"
DEFAULT_CHUNK_MIB = (DEFAULT_TUS_UPLOAD_CHUNK_MIB, 128)
USER_ENV = "TUS_BENCHMARK_USER"
PASSWORD_ENV = "TUS_BENCHMARK_PASSWORD"
TUS_TARGET_UTILIZATION = 0.80
TUS_SCENARIOS = ("adapter-ingress",)
WORKLOADS = ("large-file", "many-small-files", "resume")


@dataclass(frozen=True)
class ThroughputResult:
    chunk_mib: int
    gbit_per_second: float
    http_version: str
    mib_per_second: float
    seconds: float
    size_mib: int


@dataclass(frozen=True)
class BatchThroughputResult:
    chunk_mib: int
    concurrency: int
    gbit_per_second: float
    http_version: str
    mib_per_second: float
    payload_mib_per_upload: int
    seconds: float
    total_mib: int
    uploads: int


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _credentials_from_env() -> tuple[str, str] | None:
    user = os.getenv(USER_ENV, "")
    password = os.getenv(PASSWORD_ENV, "")
    if bool(user) != bool(password):
        raise ValueError(f"{USER_ENV} and {PASSWORD_ENV} must be set together")
    return (user, password) if user else None


def _authorization_header(credentials: tuple[str, str] | None) -> str | None:
    if credentials is None:
        return None
    encoded = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode("ascii")
    return f"Basic {encoded}"


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
    transport: TusTransport,
    base_url: str,
    *,
    size_mib: int,
    chunk_mib: int,
    resume_probe: bool = False,
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
        resumed = False
        payload = bytes(chunk_bytes)
        started = clock()
        while offset < total_bytes:
            length = min(chunk_bytes, total_bytes - offset)
            offset = transport.patch_chunk(
                upload_url,
                offset=offset,
                content=payload if length == chunk_bytes else payload[:length],
            )
            if resume_probe and not resumed:
                resumed_offset = transport.head_offset(upload_url)
                if resumed_offset != offset:
                    raise RuntimeError(
                        f"TUS resume offset changed to {resumed_offset}; expected {offset}"
                    )
                resumed = True
        elapsed = clock() - started

        return ThroughputResult(
            chunk_mib=chunk_mib,
            gbit_per_second=round(total_bytes * 8 / elapsed / 1_000_000_000, 3),
            http_version="HTTP/1.1",
            mib_per_second=round(size_mib / elapsed, 2),
            seconds=round(elapsed, 3),
            size_mib=size_mib,
        )
    finally:
        if upload_url is not None:
            transport.delete_upload(upload_url)


def _measure_batch(
    client: httpx.Client,
    transport: TusTransport,
    base_url: str,
    *,
    size_mib: int,
    chunk_mib: int,
    uploads: int,
    concurrency: int,
    resume_probe: bool,
    clock: Callable[[], float] = time.perf_counter,
) -> BatchThroughputResult:
    worker_count = min(uploads, concurrency)
    started = clock()
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tus-throughput") as pool:
        results = tuple(
            pool.map(
                lambda _index: _measure_probe(
                    client,
                    transport,
                    base_url,
                    size_mib=size_mib,
                    chunk_mib=chunk_mib,
                    resume_probe=resume_probe,
                ),
                range(uploads),
            )
        )
    elapsed = clock() - started
    total_mib = size_mib * len(results)
    return BatchThroughputResult(
        chunk_mib=chunk_mib,
        concurrency=worker_count,
        gbit_per_second=round(total_mib * 1024 * 1024 * 8 / elapsed / 1_000_000_000, 3),
        http_version="HTTP/1.1",
        mib_per_second=round(total_mib / elapsed, 2),
        payload_mib_per_upload=size_mib,
        seconds=round(elapsed, 3),
        total_mib=total_mib,
        uploads=len(results),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure a TUS endpoint without completing or publishing the synthetic upload. "
            "Results are emitted as JSON lines."
        )
    )
    parser.add_argument("url", help="TUS creation URL")
    parser.add_argument("--scenario", choices=TUS_SCENARIOS, required=True)
    parser.add_argument("--workload", choices=WORKLOADS, required=True)
    parser.add_argument(
        "--baseline-mib-per-second",
        type=_positive_float,
        required=True,
        help="raw transport baseline measured on the same path",
    )
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
        "--timeout-seconds",
        type=_positive_int,
        default=300,
        help="per-request timeout (default: 300)",
    )
    parser.add_argument(
        "--uploads",
        type=_positive_int,
        default=1,
        help="number of separately created and deleted uploads (default: 1)",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=1,
        help="concurrent uploads (default: 1)",
    )
    return parser


def _result_payload(
    result: ThroughputResult | BatchThroughputResult,
    *,
    scenario: str,
    workload: str,
    baseline_mib_per_second: float,
) -> dict[str, object]:
    return {
        **asdict(result),
        "baseline_mib_per_second": round(baseline_mib_per_second, 3),
        "scenario": scenario,
        "target_utilization": TUS_TARGET_UTILIZATION,
        "utilization": round(result.mib_per_second / baseline_mib_per_second, 4),
        "workload": workload,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.workload == "many-small-files" and args.uploads < 2:
        raise ValueError("many-small-files requires --uploads greater than one")
    chunk_sizes = args.chunk_mib or list(DEFAULT_CHUNK_MIB)
    auth = _credentials_from_env()
    tus_headers = {"User-Agent": "tus-throughput/1"}
    authorization = _authorization_header(auth)
    if authorization is not None:
        tus_headers["Authorization"] = authorization
    with httpx.Client(
        auth=auth,
        http2=True,
        timeout=float(args.timeout_seconds),
        headers={"User-Agent": "tus-throughput/1"},
    ) as client:
        with TusTransport(
            client=client,
            headers=tus_headers,
            timeout_seconds=float(args.timeout_seconds),
        ) as transport:
            _require_termination(client, args.url)
            for chunk_mib in chunk_sizes:
                result = _measure_batch(
                    client,
                    transport,
                    args.url,
                    size_mib=args.size_mib,
                    chunk_mib=chunk_mib,
                    uploads=args.uploads,
                    concurrency=args.concurrency,
                    resume_probe=args.workload == "resume",
                )
                print(
                    json.dumps(
                        _result_payload(
                            result,
                            scenario=args.scenario,
                            workload=args.workload,
                            baseline_mib_per_second=args.baseline_mib_per_second,
                        ),
                        sort_keys=True,
                    ),
                    flush=True,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
