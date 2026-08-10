from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/release.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("riverhog_release", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_release_contract(module: ModuleType, destination: Path) -> None:
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "mise.lock",
        "release.toml",
        "docker-bake.hcl",
    ):
        shutil.copy2(REPO_ROOT / relative, destination / relative)
    for source in module._workspace_pyprojects(REPO_ROOT):
        relative = source.relative_to(REPO_ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def test_release_contract_classifies_every_coordinated_distribution() -> None:
    module = load_script()

    projects = module.validate_release_contract(REPO_ROOT)

    assert len(projects) == 31
    assert {project.version for project in projects} == {"0.1.0"}
    assert Counter(project.role for project in projects) == {
        "end_user_artifact": 5,
        "deployed_implementation": 5,
        "reusable_library": 13,
        "internal_build_unit": 8,
    }
    assert {project.name for project in projects} >= {
        "jeb-client",
        "munchy-client",
        "riverhog-client",
        "riverhog-recover",
        "riverhog-server",
    }
    signing = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))["signing"]
    assert signing["checksums"] == "SHA-256"
    assert signing["signature"] == "minisign"
    assert "outside the repository, GitHub, CI logs" in signing["secret_key"]
    assert "signed by both old and new keys" in signing["rotation"]
    assert "without moving an existing tag" in signing["compromise"]
    governance = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))[
        "governance"
    ]
    assert governance["workflow_source_branch"] == "main"
    assert governance["required_check_integration_id"] == 15368
    assert governance["release"]["required_approvals"] == 0
    assert governance["tags"]["release_candidate"] == "v{version}-rc.{candidate}"
    assert governance["tags"]["final"] == "v{version}"
    assert governance["environments"] == {
        "release": "release-publication",
        "pages": "github-pages",
    }


def test_dry_run_can_write_the_same_sha_bound_summary_it_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load_script()
    payload = {"source_sha": "1" * 40, "published": False}
    monkeypatch.setattr(module, "dry_run", lambda _root, _version: payload)
    summary = tmp_path / "qualification" / "release.json"

    assert module.main(["dry-run", "--version", "1.0.0", "--summary", str(summary)]) == 0

    assert module.json.loads(summary.read_text(encoding="utf-8")) == payload
    assert module.json.loads(capsys.readouterr().out) == payload


def test_release_plan_is_exact_sha_bound_and_excludes_the_test_image() -> None:
    module = load_script()

    plan = module.build_release_plan(REPO_ROOT, "1.0.0", allow_dirty=True)

    assert plan["tag"] == "v1.0.0"
    assert len(plan["source_sha"]) == 40
    assert all(character in "0123456789abcdef" for character in plan["source_sha"])
    assert len(plan["python"]) == 31
    assert all(len(project["artifacts"]) == 2 for project in plan["python"])
    assert {image["target"] for image in plan["images"]} == set(module.RUNTIME_IMAGE_TARGETS)
    assert all(image["platforms"] == ["linux/amd64"] for image in plan["images"])
    assert all(
        image["tags"]
        == [
            f"{image['repository']}:1.0.0",
            f"{image['repository']}:sha-{plan['source_sha']}",
        ]
        for image in plan["images"]
    )
    assert "riverhog-test:dev" not in str(plan)
    assert plan["supporting_artifacts"] == {
        "documentation": "riverhog-docs-v1.0.0.tar.gz",
        "source": "riverhog-source-v1.0.0.tar.gz",
        "evidence": [
            "release-manifest.json",
            "SHA256SUMS",
            "SHA256SUMS.minisig",
            "release.spdx.json",
            "release.intoto.jsonl",
            "THIRD_PARTY_NOTICES.md",
        ],
    }
    markdown = module.render_release_markdown(plan)
    assert markdown.startswith("# Riverhog v1.0.0\n\n")
    assert f"Source: `{plan['source_sha']}`" in markdown
    assert "Initial v1 release; there is no previous release tag." in markdown


def test_coordinated_version_application_updates_all_internal_ranges(tmp_path: Path) -> None:
    module = load_script()
    _copy_release_contract(module, tmp_path)

    projects = module.apply_release_version(tmp_path, "1.0.0")

    internal_names = {project.name for project in projects}
    for pyproject in module._workspace_pyprojects(tmp_path):
        metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert metadata["version"] == "1.0.0"
        for dependency in metadata.get("dependencies", []):
            if module._dependency_name(dependency) in internal_names:
                assert dependency.endswith(">=1.0,<2.0")


def test_v1_release_rail_rejects_another_major() -> None:
    module = load_script()

    with pytest.raises(module.ReleaseError, match="only 1.x.y"):
        module.build_release_plan(REPO_ROOT, "2.0.0", allow_dirty=True)


def test_dry_run_trust_is_scoped_to_the_exact_sha_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_script()
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", "/already/trusted")

    assert module._trusted_config_paths(tmp_path) == (
        f"{tmp_path}{module.os.pathsep}/already/trusted"
    )


def test_dry_run_removes_each_temporary_image_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    tags = ["dry-run/riverhog:1.0.0", "dry-run/riverhog:sha-example"]
    remaining = set(tags)
    removed: list[str] = []

    monkeypatch.setattr(
        module,
        "_docker_image_exists",
        lambda tag, *, cwd: cwd == tmp_path and tag in remaining,
    )

    def remove(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["docker", "image", "rm"]
        tag = command[3]
        remaining.remove(tag)
        removed.append(tag)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", remove)

    module._remove_release_image_tags(tags, cwd=tmp_path)

    assert removed == list(reversed(tags))
    assert remaining == set()


def test_source_archive_is_deterministic_and_commit_time_normalized(tmp_path: Path) -> None:
    module = load_script()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "README.md").write_text("Riverhog\n", encoding="utf-8")
    script = checkout / "run"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    module._write_source_archive(checkout, first, version="1.0.0", source_epoch=1234567890)
    module._write_source_archive(checkout, second, version="1.0.0", source_epoch=1234567890)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, mode="r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == [
        "riverhog-1.0.0",
        "riverhog-1.0.0/README.md",
        "riverhog-1.0.0/run",
    ]
    assert all(member.mtime == 1234567890 for member in members)
    assert all(member.uid == 0 and member.gid == 0 for member in members)
    assert members[-1].mode == 0o755


def test_release_evidence_is_complete_and_minisign_verified(tmp_path: Path) -> None:
    module = load_script()
    output = tmp_path / "evidence"
    output.mkdir()
    payload = output / "artifact.whl"
    payload.write_bytes(b"release artifact\n")
    keys = tmp_path / "keys"
    keys.mkdir()
    public_key = keys / "release.pub"
    signing_key = keys / "release.key"
    subprocess.run(
        [
            "minisign",
            "-G",
            "-W",
            "-p",
            str(public_key),
            "-s",
            str(signing_key),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    records = [
        {
            "kind": "wheel",
            "name": "artifact.whl",
            "sha256": module._sha256_file(payload),
            "size": payload.stat().st_size,
            "distribution": "riverhog-client",
            "version": "1.0.0",
            "license": "CAL-1.0",
            "dependencies": [],
            "_components": [
                {
                    "kind": "python",
                    "name": "riverhog-protocol",
                    "version": "1.0.0",
                    "license": "CAL-1.0",
                }
            ],
        }
    ]

    verification = module._generate_release_evidence(
        REPO_ROOT,
        output,
        records,
        version="1.0.0",
        source_sha="1" * 40,
        spdx_created="2009-02-13T23:31:30Z",
        signing_key=signing_key,
        public_key=public_key,
    )

    assert verification["subjects"] == 1
    assert verification["signature_verified"] is True
    assert (output / "SHA256SUMS.minisig").is_file()
    assert (output / records[0]["sbom"]).is_file()
    manifest = module.json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["published"] is False
    assert manifest["subjects"] == records
