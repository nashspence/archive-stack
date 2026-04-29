from __future__ import annotations

from pathlib import Path

from arc_core.planner.manifest import MANIFEST_FILENAME, README_FILENAME
from tests.fixtures.data import IMAGE_FIXTURES, write_tree
from tests.fixtures.disc_contracts import inspect_fixture_image_root


def _relative_dirs(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())


def _relative_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def test_inspect_fixture_image_root_reads_fixture_tree_without_xorriso(tmp_path: Path) -> None:
    fixture = IMAGE_FIXTURES[0]
    image_root = write_tree(tmp_path / "image-root", fixture.files)

    inspected = inspect_fixture_image_root(
        image_id=fixture.volume_id,
        image_root=image_root,
        iso_bytes=b"fixture iso bytes",
        workspace=tmp_path,
    )

    assert inspected.iso_path.read_bytes() == b"fixture iso bytes"
    assert inspected.extract_root != image_root
    assert inspected.files == _relative_files(image_root)
    assert inspected.directories == _relative_dirs(image_root)
    assert inspected.readme == (image_root / README_FILENAME).read_text(encoding="utf-8")
    assert inspected.disc_manifest["image"]["id"] == fixture.volume_id
    assert (inspected.extract_root / README_FILENAME).is_file()
    assert (inspected.extract_root / MANIFEST_FILENAME).is_file()
