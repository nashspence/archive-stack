from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from riverhog_recover.recovery import RecoveryError, recover_archive


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riverhog-recover",
        description="Recover one complete Riverhog archive copy without Riverhog.",
    )
    parser.add_argument("archive", type=Path, help="downloaded opaque archive directory")
    parser.add_argument("output", type=Path, help="new directory for recovered files")
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        help="read the archive passphrase from a permission-restricted file",
    )
    parser.add_argument(
        "--minisign-public-key",
        type=Path,
        help="verify SHA256SUMS with this independently trusted public key",
    )
    parser.add_argument("--age-command", default="age", help=argparse.SUPPRESS)
    parser.add_argument("--minisign-command", default="minisign", help=argparse.SUPPRESS)
    return parser


def _passphrase(path: Path | None) -> str:
    if path is None:
        value = getpass.getpass("Archive passphrase: ")
    else:
        try:
            value = path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise RecoveryError(f"cannot read passphrase file: {exc}") from exc
    if not value:
        raise RecoveryError("archive passphrase is empty")
    return value


def main() -> None:
    args = _parser().parse_args()
    try:
        summary = recover_archive(
            args.archive,
            args.output,
            passphrase=_passphrase(args.passphrase_file),
            age_command=args.age_command,
            minisign_public_key=args.minisign_public_key,
            minisign_command=args.minisign_command,
        )
    except RecoveryError as exc:
        print(f"riverhog-recover: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        f"Recovered {summary.files} files ({summary.bytes} bytes) to {summary.output}",
        flush=True,
    )


if __name__ == "__main__":
    main()
