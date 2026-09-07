from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from riverhog_recover.recovery import (
    RecoveryError,
    read_recovery_descriptor,
    recover_archive,
    recover_collection_description,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riverhog-recover",
        description="Recover one complete Riverhog archive copy without Riverhog.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=importlib.metadata.version("riverhog-recover"),
    )
    parser.add_argument("archive", type=Path, help="downloaded opaque archive directory")
    parser.add_argument("output", type=Path, nargs="?", help="new directory for recovered files")
    parser.add_argument(
        "--description-only",
        action="store_true",
        help="validate and emit only description.json.age without reading collection payloads",
    )
    parser.add_argument(
        "--passphrases-file",
        type=Path,
        help="read an opaque key-ID to passphrase JSON map from a permission-restricted file",
    )
    parser.add_argument("--age-command", default="age", help=argparse.SUPPRESS)
    return parser


def _passphrases(path: Path | None, *, archive: Path) -> dict[str, str]:
    descriptor = read_recovery_descriptor(archive)
    passphrase_id = descriptor.encryption.passphrase_id
    if path is None:
        value = getpass.getpass(f"Archive passphrase ({passphrase_id}): ")
        if not value:
            raise RecoveryError(f"archive passphrase is empty for key ID {passphrase_id}")
        return {passphrase_id: value}
    try:
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise RecoveryError("passphrases file must not be accessible by group or others")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except RecoveryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"cannot read passphrases file: {exc}") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value for key, value in payload.items()
    ):
        raise RecoveryError("passphrases file must contain a string-to-string JSON object")
    return payload


def main() -> None:
    args = _parser().parse_args()
    try:
        passphrases = _passphrases(args.passphrases_file, archive=args.archive)
        if args.description_only:
            if args.output is not None:
                raise RecoveryError("output must be omitted with --description-only")
            description = recover_collection_description(
                args.archive,
                passphrases=passphrases,
                age_command=args.age_command,
            )
            print(
                "null" if description is None else description.to_json_bytes().decode("utf-8"),
                flush=True,
            )
            return
        if args.output is None:
            raise RecoveryError("output is required unless --description-only is used")
        summary = recover_archive(
            args.archive,
            args.output,
            passphrases=passphrases,
            age_command=args.age_command,
        )
    except RecoveryError as exc:
        print(f"riverhog-recover: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        f"Recovered {summary.files} files ({summary.bytes} bytes) to {summary.output}; "
        f"provenance={summary.provenance_mode} journals={summary.provenance_journals}",
        flush=True,
    )


if __name__ == "__main__":
    main()
