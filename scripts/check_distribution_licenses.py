from __future__ import annotations

import argparse
import email
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExpectedDistribution:
    name: str
    license: str
    license_text: bytes


def _workspace_projects(root: Path) -> dict[str, ExpectedDistribution]:
    workspace = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    projects: dict[str, ExpectedDistribution] = {}
    for pattern in workspace["tool"]["uv"]["workspace"]["members"]:
        for member in root.glob(pattern):
            pyproject = member / "pyproject.toml"
            if not pyproject.is_file():
                raise ValueError(f"workspace member lacks pyproject.toml: {member}")
            project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
            name = str(project["name"])
            license_expression = str(project["license"])
            projects[name] = ExpectedDistribution(
                name=name,
                license=license_expression,
                license_text=(member / "LICENSE").read_bytes(),
            )
    return projects


def check_distributions(root: Path, dist_dir: Path) -> None:
    expected = _workspace_projects(root)
    found: set[str] = set()
    wheels = sorted(dist_dir.glob("*.whl"))
    if not wheels:
        raise ValueError(f"no wheels found in {dist_dir}")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError(f"{wheel.name}: expected exactly one METADATA file")
            metadata_name = metadata_names[0]
            metadata = email.message_from_bytes(archive.read(metadata_name))
            name = str(metadata["Name"])
            current = expected.get(name)
            if current is None:
                raise ValueError(f"{wheel.name}: unexpected distribution {name}")
            if name in found:
                raise ValueError(f"multiple wheels found for {name}")
            found.add(name)
            if metadata["License-Expression"] != current.license:
                raise ValueError(f"{wheel.name}: License-Expression is not {current.license}")
            license_files = metadata.get_all("License-File", [])
            if license_files != ["LICENSE"]:
                raise ValueError(f"{wheel.name}: License-File is not exactly LICENSE")
            dist_info = metadata_name.removesuffix("METADATA")
            license_member = f"{dist_info}licenses/LICENSE"
            if license_member not in archive.namelist():
                raise ValueError(f"{wheel.name}: applicable license text is missing")
            if archive.read(license_member) != current.license_text:
                raise ValueError(f"{wheel.name}: applicable license text drifted")
    missing = set(expected) - found
    if missing:
        raise ValueError(f"missing wheels: {', '.join(sorted(missing))}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", nargs="?", default="dist", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        check_distributions(root, args.dist_dir.resolve())
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"distribution license check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("All workspace wheels contain their declared license.")


if __name__ == "__main__":
    main()
