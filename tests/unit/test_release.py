from __future__ import annotations

import ast
import importlib.util
import json
import os
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
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
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
    for image in module.RUNTIME_IMAGE_TARGETS:
        source = module._bake_dockerfile(REPO_ROOT, image)
        relative = source.relative_to(REPO_ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def test_release_contract_classifies_every_coordinated_distribution() -> None:
    module = load_script()

    projects = module.validate_release_contract(REPO_ROOT)

    assert len(projects) == 51
    assert {project.version for project in projects} == {"0.1.0"}
    assert Counter(project.role for project in projects) == {
        "end_user_artifact": 4,
        "deployed_implementation": 13,
        "reusable_library": 30,
        "internal_build_unit": 4,
    }
    assert {project.name for project in projects} >= {
        "riverhog-client",
        "riverhog-ftp-adapter",
        "riverhog-ftp-adapter-api-client",
        "riverhog-recover",
        "riverhog-server",
        "riverhog-storage-adapter-aws",
        "riverhog-storage-adapter-backblaze",
        "stove0-server",
        "stove0-client",
        "stove0-exiftool-observer",
        "stove0-ffprobe-sampling-observer",
        "stove0-nvenc-av1-opus-target",
        "stove0-nvenc-av1-opus-review-sampler",
        "stove0-opus-target",
        "stove0-opus-review-sampler",
        "stove0-review-target",
        "stove0-api-client",
        "stove0-observer-protocol",
        "stove0-observer-client",
        "stove0-observer-support",
        "stove0-operator-contracts",
        "stove0-protocol",
        "stove0-recipe-config",
        "stove0-review-contracts",
        "stove0-review-sampler-client",
        "stove0-review-sampler-protocol",
        "stove0-review-sampler-support",
        "stove0-target-protocol",
        "stove0-target-client",
        "stove0-target-support",
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
    assert governance["branch_delivery"] == "pre-v1-lockstep"
    assert governance["required_check_integration_id"] == 15368
    assert governance["release"]["required_approvals"] == 0
    assert governance["tags"]["release_candidate"] == "v{version}-rc.{candidate}"
    assert governance["tags"]["final"] == "v{version}"
    assert governance["environments"] == {
        "release": "release-publication",
        "pages": "github-pages",
        "provider_qualification_provisioning": "provider-qualification-provisioning",
        "provider_qualification_runtime": "provider-qualification",
    }
    platforms = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))["platforms"]
    assert platforms == {
        "end_user_artifacts": ["linux-x64", "macos-arm64", "windows-x64"],
        "deployed_implementations": ["linux/amd64"],
    }


def test_reusable_library_public_annotations_do_not_leak_internal_build_units() -> None:
    release = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))
    internal_modules = {
        Path(path).name.replace("-", "_") for path in release["python"]["internal_build_unit"]
    }
    failures: list[str] = []

    def annotation_leaks(annotation: ast.expr | None, imports: dict[str, str]) -> bool:
        if annotation is None:
            return False
        return any(
            isinstance(node, ast.Name) and node.id in imports for node in ast.walk(annotation)
        )

    def check_function(
        source: Path,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        imports: dict[str, str],
    ) -> None:
        annotations = [
            *(argument.annotation for argument in (*node.args.posonlyargs, *node.args.args)),
            *(argument.annotation for argument in node.args.kwonlyargs),
            node.args.vararg.annotation if node.args.vararg else None,
            node.args.kwarg.annotation if node.args.kwarg else None,
            node.returns,
        ]
        if any(annotation_leaks(annotation, imports) for annotation in annotations):
            failures.append(f"{source.relative_to(REPO_ROOT)}:{node.lineno} {node.name}")

    for relative in release["python"]["reusable_library"]:
        for source in sorted((REPO_ROOT / relative).glob("src/**/*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            imports: dict[str, str] = {}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    if root in internal_modules:
                        for name in node.names:
                            imports[name.asname or name.name] = root
                elif isinstance(node, ast.Import):
                    for name in node.names:
                        root = name.name.split(".", 1)[0]
                        if root in internal_modules:
                            imports[name.asname or root] = root
            if not imports:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_"):
                        check_function(source, node, imports)
                elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if not member.name.startswith("_"):
                                check_function(source, member, imports)
                        elif (
                            isinstance(member, ast.AnnAssign)
                            and isinstance(member.target, ast.Name)
                            and not member.target.id.startswith("_")
                            and annotation_leaks(member.annotation, imports)
                        ):
                            failures.append(
                                f"{source.relative_to(REPO_ROOT)}:{member.lineno} "
                                f"{node.name}.{member.target.id}"
                            )

    assert failures == []


def test_image_notices_require_the_exact_locked_standalone_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    payload = {
        "python": [],
        "deb": [],
        "standalone": [
            {
                "kind": "standalone",
                "name": "minisign",
                "version": "0.11",
                "notices": [
                    {
                        "source": "/usr/share/licenses/minisign/LICENSE",
                        "content": module.base64.b64encode(b"license\n").decode("ascii"),
                    }
                ],
            }
        ],
        "missing": [],
    }
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload)
        ),
    )

    with pytest.raises(module.ReleaseError, match="differs from its exact mise lock"):
        module._image_notice_components(
            REPO_ROOT,
            "example:test",
            first_party=set(),
            expected_standalone={"minisign": "0.12"},
        )


def test_image_notices_accept_exact_locked_standalone_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    payload = {
        "python": [],
        "deb": [],
        "standalone": [
            {
                "kind": "standalone",
                "name": "minisign",
                "version": "0.12",
                "notices": [
                    {
                        "source": "/usr/share/licenses/minisign/LICENSE",
                        "content": module.base64.b64encode(b"license\n").decode("ascii"),
                    }
                ],
            }
        ],
        "missing": [],
    }
    monkeypatch.setattr(
        module,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(payload)
        ),
    )

    assert module._image_notice_components(
        REPO_ROOT,
        "example:test",
        first_party=set(),
        expected_standalone={"minisign": "0.12"},
    ) == [
        {
            "kind": "standalone",
            "name": "minisign",
            "version": "0.12",
            "notices": [
                {
                    "source": "/usr/share/licenses/minisign/LICENSE",
                    "content": b"license\n",
                }
            ],
        }
    ]


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
    assert len(plan["python"]) == 51
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
        "installation": {
            "manifest": "install-manifest.json",
            "locks": [
                "pylock.gogurt.toml",
                "pylock.riverhog-client.toml",
                "pylock.riverhog-recover.toml",
                "pylock.stove0-client.toml",
            ],
            "index_snapshot": "riverhog-python-index-v1.0.0.tar.gz",
            "gogurt_listener_reference": "gogurt-listener-v1.0.0.md",
        },
        "notices": {
            "schema": "riverhog-artifact-notices/v1",
            "directory": "notices",
            "format": "tar.gz",
            "basis": "exact-artifact-contents",
            "required_for": ["wheel", "image"],
        },
        "evidence": [
            "install-manifest.json",
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
    original_lock = tomllib.loads((tmp_path / "uv.lock").read_text(encoding="utf-8"))

    projects = module.apply_release_version(tmp_path, "1.0.0")

    internal_names = {project.name for project in projects}
    for pyproject in module._workspace_pyprojects(tmp_path):
        metadata = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert metadata["version"] == "1.0.0"
        for dependency in metadata.get("dependencies", []):
            if module._dependency_name(dependency) in internal_names:
                assert dependency.endswith(">=1.0,<2.0")
    updated_lock = tomllib.loads((tmp_path / "uv.lock").read_text(encoding="utf-8"))
    original_external = [
        package for package in original_lock["package"] if package["name"] not in internal_names
    ]
    updated_external = [
        package for package in updated_lock["package"] if package["name"] not in internal_names
    ]
    assert updated_external == original_external
    assert {
        package["name"]: package["version"]
        for package in updated_lock["package"]
        if package["name"] in internal_names
    } == dict.fromkeys(internal_names, "1.0.0")
    for package in updated_lock["package"]:
        for requirement in package.get("metadata", {}).get("requires-dist", []):
            if requirement["name"] in internal_names and "specifier" in requirement:
                assert requirement["specifier"] == ">=1.0,<2.0"

    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(tmp_path / "empty-uv-cache")
    subprocess.run(
        ["uv", "lock", "--offline", "--check"],
        cwd=tmp_path,
        check=True,
        env=environment,
    )


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


def test_artifact_notice_bundle_is_deterministic_and_subject_bound(tmp_path: Path) -> None:
    module = load_script()
    record = {
        "kind": "wheel",
        "name": "python/riverhog_client-1.0.0-py3-none-any.whl",
        "sha256": "2" * 64,
    }
    components = [
        {
            "kind": "python",
            "name": "riverhog-client",
            "version": "1.0.0",
            "notices": [
                {
                    "source": "riverhog_client-1.0.0.dist-info/licenses/LICENSE",
                    "content": b"license text\n",
                }
            ],
        }
    ]
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    module._write_notice_bundle(first, record, components, source_epoch=1234567890)
    module._write_notice_bundle(second, record, components, source_epoch=1234567890)

    assert first.read_bytes() == second.read_bytes()
    assert module._verify_notice_bundle(first, record) == 1
    with tarfile.open(first, mode="r:gz") as archive:
        index_stream = archive.extractfile("NOTICE.json")
        assert index_stream is not None
        index = json.loads(index_stream.read())
    assert index["schema"] == "riverhog-artifact-notices/v1"
    assert index["subject"] == record


def test_required_artifact_notice_input_fails_closed(tmp_path: Path) -> None:
    module = load_script()
    record = {
        "kind": "wheel",
        "name": "python/artifact.whl",
        "sha256": "3" * 64,
    }

    with pytest.raises(module.ReleaseError, match="notice input is absent"):
        module._write_subject_notices(tmp_path, [record], source_epoch=1234567890)


def test_generated_install_reference_has_a_file_sbom(tmp_path: Path) -> None:
    module = load_script()
    record = {
        "kind": "install-reference",
        "name": "installation/gogurt-listener-v1.0.0.md",
        "sha256": "4" * 64,
        "version": "1.0.0",
        "license": "Apache-2.0",
    }

    module._write_subject_sboms(
        tmp_path,
        [record],
        source_sha="1" * 40,
        created="2009-02-13T23:31:30Z",
    )

    sbom = json.loads((tmp_path / record["sbom"]).read_text(encoding="utf-8"))
    assert sbom["packages"][0]["primaryPackagePurpose"] == "FILE"


def test_release_evidence_is_complete_and_minisign_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            "_notice_components": [
                {
                    "kind": "python",
                    "name": "riverhog-client",
                    "version": "1.0.0",
                    "notices": [
                        {
                            "source": "riverhog_client.dist-info/licenses/LICENSE",
                            "content": b"CAL\n",
                        }
                    ],
                }
            ],
        }
    ]
    install_manifest = {"schema": "riverhog-installation/v1"}
    (output / "install-manifest.json").write_text(
        module.json.dumps(install_manifest), encoding="utf-8"
    )
    monkeypatch.setattr(
        module.installation,
        "verify_installation_artifacts",
        lambda _output, _manifest: None,
    )

    verification = module._generate_release_evidence(
        REPO_ROOT,
        output,
        records,
        version="1.0.0",
        source_sha="1" * 40,
        source_epoch=1234567890,
        spdx_created="2009-02-13T23:31:30Z",
        install_manifest=install_manifest,
        signing_key=signing_key,
        public_key=public_key,
    )

    assert verification["subjects"] == 1
    assert verification["notice_components"] == 1
    assert verification["signature_verified"] is True
    assert (output / "SHA256SUMS.minisig").is_file()
    assert (output / records[0]["sbom"]).is_file()
    assert (output / records[0]["notices"]).is_file()
    manifest = module.json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["published"] is False
    assert manifest["subjects"] == records
