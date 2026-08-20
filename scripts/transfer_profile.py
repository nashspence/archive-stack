#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

MIB = 1024 * 1024
NETWORK_TARGET = 0.90

SCENARIO_OPERATIONS: Mapping[str, frozenset[str]] = {
    "riverhog-ingress": frozenset(
        {"pack_upload_open", "pack_upload_part", "raw_upload_open", "raw_upload_part"}
    ),
    "stove0-derived-publication": frozenset(
        {"pack_upload_open", "pack_upload_part", "raw_upload_open", "raw_upload_part"}
    ),
    "stove0-input-read": frozenset(
        {"pack_retrieval_member", "pack_retrieval_range", "raw_retrieval_part"}
    ),
    "riverhog-retrieval": frozenset(
        {"pack_retrieval_member", "pack_retrieval_range", "raw_retrieval_part"}
    ),
    "reference-recovery": frozenset(),
    "archive-upload": frozenset(
        {"pack_upload_open", "pack_upload_part", "raw_upload_open", "raw_upload_part"}
    ),
    "archive-retrieval": frozenset(
        {
            "pack_retrieval_member",
            "pack_retrieval_range",
            "raw_retrieval_part",
            "retrieval_cache_hydration",
        }
    ),
    "archive-replication": frozenset({"archive_copy_part", "archive_copy_object"}),
}
NETWORK_SCENARIOS = frozenset(SCENARIO_OPERATIONS) - {"reference-recovery"}
WORKLOADS = ("large-file", "many-small-files", "resume")
_FIELD_RE = re.compile(r"([a-z_]+)=([^ ]+)")


@dataclass(frozen=True)
class TransferLogSummary:
    bottlenecks: dict[str, int]
    operations: dict[str, int]
    phase_seconds: dict[str, float]
    plaintext_bytes: int
    records: int
    stored_bytes: int


@dataclass(frozen=True)
class ProfileResult:
    baseline_mib_per_second: float | None
    elapsed_seconds: float
    items: int
    items_per_second: float
    mib_per_second: float
    payload_bytes: int
    scenario: str
    target_utilization: float | None
    seconds_per_item: float
    transfer_log: TransferLogSummary | None
    utilization: float | None
    workload: str


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


def _parse_transfer_line(line: str) -> dict[str, str] | None:
    marker = "transfer operation="
    position = line.find(marker)
    if position < 0:
        return None
    fields = dict(_FIELD_RE.findall(line[position + len("transfer ") :]))
    return fields if fields.get("operation") else None


def summarize_transfer_log(
    text: str,
    *,
    expected_operations: frozenset[str],
) -> TransferLogSummary:
    operations: Counter[str] = Counter()
    bottlenecks: Counter[str] = Counter()
    phases: Counter[str] = Counter()
    plaintext_bytes = 0
    stored_bytes = 0
    for line in text.splitlines():
        fields = _parse_transfer_line(line)
        if fields is None or fields["operation"] not in expected_operations:
            continue
        operations[fields["operation"]] += 1
        bottlenecks[fields.get("bottleneck", "unknown")] += 1
        plaintext_bytes += int(fields["plaintext_bytes"])
        stored_bytes += int(fields["stored_bytes"])
        for phase in (
            "queue",
            "source",
            "integrity",
            "crypto",
            "processing",
            "remote",
            "checkpoint",
            "downstream",
        ):
            phases[phase] += float(fields.get(f"{phase}_seconds", "0"))
    records = sum(operations.values())
    if not records:
        names = ", ".join(sorted(expected_operations))
        raise ValueError(f"transfer log contains no expected operations: {names}")
    return TransferLogSummary(
        bottlenecks=dict(sorted(bottlenecks.items())),
        operations=dict(sorted(operations.items())),
        phase_seconds={name: round(phases[name], 6) for name in sorted(phases)},
        plaintext_bytes=plaintext_bytes,
        records=records,
        stored_bytes=stored_bytes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one supported transfer or recovery command and emit a secret-free JSON "
            "goodput profile. The command and its arguments are never copied into the result."
        )
    )
    parser.add_argument("--scenario", choices=sorted(SCENARIO_OPERATIONS), required=True)
    parser.add_argument("--workload", choices=WORKLOADS, required=True)
    parser.add_argument("--payload-bytes", type=_positive_int, required=True)
    parser.add_argument("--items", type=_positive_int, default=1)
    parser.add_argument(
        "--baseline-mib-per-second",
        type=_positive_float,
        help="raw transport baseline measured on the same path",
    )
    parser.add_argument(
        "--target-utilization",
        type=_positive_float,
        default=NETWORK_TARGET,
        help="qualification target as a baseline fraction (default: 0.90)",
    )
    parser.add_argument(
        "--transfer-log",
        type=Path,
        help="server log window containing identity-safe riverhog.transfer records",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to profile, preceded by --",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    if args.scenario in NETWORK_SCENARIOS and args.baseline_mib_per_second is None:
        parser.error("network scenarios require --baseline-mib-per-second")
    if args.target_utilization > 1:
        parser.error("--target-utilization must be at most 1")
    expected = SCENARIO_OPERATIONS[args.scenario]
    if args.transfer_log is not None and not expected:
        parser.error("reference-recovery has no server transfer log")
    return command


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command = _validate_args(args, parser)
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode:
        return completed.returncode

    mib_per_second = args.payload_bytes / MIB / elapsed
    baseline = args.baseline_mib_per_second
    utilization = mib_per_second / baseline if baseline is not None else None
    log_summary = None
    if args.transfer_log is not None:
        log_summary = summarize_transfer_log(
            args.transfer_log.read_text(encoding="utf-8"),
            expected_operations=SCENARIO_OPERATIONS[args.scenario],
        )
    result = ProfileResult(
        baseline_mib_per_second=round(baseline, 3) if baseline is not None else None,
        elapsed_seconds=round(elapsed, 6),
        items=args.items,
        items_per_second=round(args.items / elapsed, 3),
        mib_per_second=round(mib_per_second, 3),
        payload_bytes=args.payload_bytes,
        scenario=args.scenario,
        target_utilization=args.target_utilization if baseline is not None else None,
        seconds_per_item=round(elapsed / args.items, 6),
        transfer_log=log_summary,
        utilization=round(utilization, 4) if utilization is not None else None,
        workload=args.workload,
    )
    print(json.dumps(asdict(result), sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
