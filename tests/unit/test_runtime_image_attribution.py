from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from runtime_image_attribution import (  # noqa: E402
    RuntimeAttributionError,
    checked_attribution_sources,
    locked_runtime_payloads,
    locked_tool_versions,
)


def test_mise_config_and_lock_expose_one_exact_version_per_tool() -> None:
    assert locked_tool_versions(REPO_ROOT) == {
        "age": "1.3.1",
        "exiftool": "13.59",
        "minisign": "0.12",
        "python": "3.12.3",
        "rclone": "1.75.0",
        "uv": "0.11.24",
    }


def test_runtime_payload_attribution_is_derived_from_final_stage_copies() -> None:
    observed = {}
    for dockerfile in REPO_ROOT.rglob("Dockerfile"):
        if dockerfile == REPO_ROOT / "tests/Dockerfile":
            continue
        payloads = locked_runtime_payloads(REPO_ROOT, dockerfile)
        if payloads:
            observed[str(dockerfile.relative_to(REPO_ROOT))] = payloads

    assert observed == {
        "reference/stove0/observers/exiftool/Dockerfile": {"exiftool": "13.59"},
        "reference/stove0/targets/review/rclone-effect-target/Dockerfile": {"rclone": "1.75.0"},
    }


def test_runtime_derivation_ignores_build_only_tools(tmp_path: Path) -> None:
    (tmp_path / "mise.toml").write_text(
        '[tools]\nminisign = "0.12"\nuv = "0.11.24"\n', encoding="utf-8"
    )
    (tmp_path / "mise.lock").write_text(
        '[[tools.minisign]]\nversion = "0.12"\n[[tools.uv]]\nversion = "0.11.24"\n',
        encoding="utf-8",
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python AS build\n"
        "COPY --from=locked-tools /opt/riverhog-tools/bin/uv /usr/local/bin/uv\n"
        "FROM python\n"
        "COPY --from=locked-tools /opt/riverhog-tools/bin/minisign /usr/local/bin/minisign\n"
        "COPY --from=locked-tools /opt/riverhog-tools/licenses/minisign/0.12/LICENSE "
        "/usr/share/licenses/riverhog-third-party/minisign/0.12/LICENSE\n",
        encoding="utf-8",
    )

    assert locked_runtime_payloads(tmp_path, dockerfile) == {"minisign": "0.12"}


def test_runtime_derivation_rejects_mise_config_lock_version_mismatch(tmp_path: Path) -> None:
    (tmp_path / "mise.toml").write_text('[tools]\nminisign = "0.12"\n', encoding="utf-8")
    (tmp_path / "mise.lock").write_text('[[tools.minisign]]\nversion = "0.11"\n', encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python\n", encoding="utf-8")

    with pytest.raises(RuntimeAttributionError, match="config and lock versions differ"):
        locked_runtime_payloads(tmp_path, dockerfile)


def test_checked_attribution_text_matches_its_locked_version_and_source_digest() -> None:
    locked = locked_tool_versions(REPO_ROOT)
    sources = checked_attribution_sources(REPO_ROOT)
    assert sources
    accounted: set[Path] = set()

    for source in sources:
        metadata_path = Path(source["metadata_path"])
        component = str(source["component"])
        version = str(source["version"])
        assert source["schema"] == "riverhog-third-party-attribution-source/v1"
        assert metadata_path.parent.parts[-2:] == (component, version)
        assert locked[component] == version
        assert str(source["url"]).startswith("https://")
        attribution = metadata_path.parent / str(source["file"])
        assert attribution.parent == metadata_path.parent
        assert hashlib.sha256(attribution.read_bytes()).hexdigest() == source["sha256"]
        accounted.update((metadata_path, attribution))

    assert accounted == {path for path in (REPO_ROOT / "third_party").rglob("*") if path.is_file()}
