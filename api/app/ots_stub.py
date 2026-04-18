from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "stamp":
        print("usage: python -m app.ots_stub stamp <manifest-path>", file=sys.stderr)
        return 2

    manifest_path = Path(args[1])
    if not manifest_path.exists():
        print("manifest path not found", file=sys.stderr)
        return 1

    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    proof_path = manifest_path.with_name(f"{manifest_path.name}.ots")
    proof_path.write_text(
        "\n".join(
            [
                "OpenTimestamps stub proof v1",
                f"file: {manifest_path.name}",
                f"sha256: {digest}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
