from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = REPO_ROOT / ".compose"
DEFAULT_IMAGE = "archive-stack-test:dev"
GENERATED_PROD_HARNESS_STATE_RE = re.compile(
    r"^archive-stack-test-[a-z0-9]+(?:-[a-z0-9]+)*-\d+$"
)


def is_generated_prod_harness_state_name(name: str) -> bool:
    return GENERATED_PROD_HARNESS_STATE_RE.fullmatch(name) is not None


def select_generated_prod_harness_state_roots(state_root: Path) -> list[Path]:
    if not state_root.exists():
        return []
    return sorted(
        path
        for path in state_root.iterdir()
        if path.is_dir() and is_generated_prod_harness_state_name(path.name)
    )


def _container_path_for(root_dir: Path, target: Path) -> str:
    relative = target.resolve().relative_to(root_dir.resolve())
    return f"/app/{relative.as_posix()}"


def _delete_with_docker(*, root_dir: Path, image: str, target: Path) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{root_dir}:/app",
            "--entrypoint",
            "rm",
            image,
            "-rf",
            _container_path_for(root_dir, target),
        ],
        check=True,
    )


def _relative_display(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List or delete generated prod-harness compose state roots under .compose/."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete the listed generated state roots using Docker-backed rm",
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    state_root = args.state_root
    roots = select_generated_prod_harness_state_roots(state_root)
    if not roots:
        print(f"No generated prod-harness state roots found under {_relative_display(state_root)}.")
        return 0

    for root in roots:
        action = "Deleting" if args.force else "Would delete"
        print(f"{action} {_relative_display(root)}")

    if not args.force:
        print("Run again with args='--force' to prune the listed roots.")
        return 0

    for root in roots:
        _delete_with_docker(root_dir=REPO_ROOT, image=args.image, target=root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
