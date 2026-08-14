from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
CODEQL_WORKFLOW = REPO_ROOT / ".github/workflows/codeql.yml"
QUALIFICATION_WORKFLOW = REPO_ROOT / ".github/workflows/release-qualification.yml"
PROVIDER_QUALIFICATION_WORKFLOW = REPO_ROOT / ".github/workflows/provider-qualification.yml"
MISE_LOCK = REPO_ROOT / "mise.lock"


def test_codeql_covers_every_governed_branch_with_stable_checks() -> None:
    text = CODEQL_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"] == {
        "pull_request": {"branches": ["main", "release/v1"]},
        "push": {"branches": ["main", "release/v1"]},
        "schedule": [{"cron": "41 6 * * 4"}],
        "workflow_dispatch": "",
    }
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
        "packages": "read",
        "security-events": "write",
    }
    assert workflow["concurrency"] == {
        "group": "codeql-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": "true",
    }
    assert set(workflow["jobs"]) == {"analyze"}
    job = workflow["jobs"]["analyze"]
    assert job["name"] == "Analyze (${{ matrix.language }})"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["strategy"] == {
        "fail-fast": "false",
        "matrix": {"language": ["actions", "python"]},
    }
    steps = job["steps"]
    assert [step["uses"].split("@", 1)[0] for step in steps] == [
        "actions/checkout",
        "github/codeql-action/init",
        "github/codeql-action/analyze",
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in steps)
    assert steps[0]["with"] == {"persist-credentials": "false"}
    assert steps[1]["with"] == {
        "languages": "${{ matrix.language }}",
        "queries": "security-extended",
    }
    assert steps[2]["with"] == {"category": "/language:${{ matrix.language }}"}


def test_ci_uses_thin_repository_and_image_build_adapters() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"] == {
        "pull_request": "",
        "push": {"branches": ["main", "release/v1"]},
        "workflow_dispatch": "",
        "workflow_call": {
            "inputs": {
                "ref": {
                    "description": "Exact commit to check out and validate.",
                    "required": "false",
                    "type": "string",
                }
            }
        },
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"

    assert set(workflow["jobs"]) == {
        "repository",
        "client-platforms",
        "images",
    }
    job = workflow["jobs"]["repository"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["strategy"]["fail-fast"] == "false"
    matrix = job["strategy"]["matrix"]["include"]
    assert [entry["target"] for entry in matrix] == [
        "lint",
        "compile",
        "unit",
        "spec",
        "c2sp-vectors",
        "postgres-concurrency",
        "compose-smoke",
        "dist-smoke",
    ]
    assert [entry["target"] for entry in matrix if entry.get("docker") == "true"] == [
        "postgres-concurrency",
        "compose-smoke",
    ]

    steps = job["steps"]
    assert [step["uses"].split("@", 1)[0] for step in steps if "uses" in step] == [
        "actions/checkout",
        "jdx/mise-action",
        "docker/setup-docker-action",
        "docker/setup-compose-action",
    ]
    action_steps = [
        step
        for workflow_job in workflow["jobs"].values()
        for step in workflow_job["steps"]
        if "uses" in step
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)
    checkout_steps = [
        step
        for workflow_job in workflow["jobs"].values()
        for step in workflow_job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert all(
        step["with"]
        == {
            "persist-credentials": "false",
            "ref": "${{ inputs.ref || github.sha }}",
        }
        for step in checkout_steps
    )
    assert steps[2]["if"] == "matrix.docker"
    assert steps[2]["with"]["version"] == "v29.3.1"
    assert json.loads(steps[2]["with"]["daemon-config"]) == {
        "features": {"containerd-snapshotter": True}
    }
    assert steps[3]["if"] == "matrix.docker"
    assert steps[3]["with"] == {"version": "v5.1.1"}
    assert [step["run"] for step in steps if "run" in step] == ['make "$CI_TARGET"']
    assert steps[-1]["env"] == {"CI_TARGET": "${{ matrix.target }}"}

    client_platforms = workflow["jobs"]["client-platforms"]
    assert client_platforms["strategy"] == {
        "fail-fast": "false",
        "matrix": {
            "os": ["ubuntu-24.04", "macos-15", "windows-2025"],
        },
    }
    assert client_platforms["runs-on"] == "${{ matrix.os }}"
    assert [
        step["uses"].split("@", 1)[0] for step in client_platforms["steps"] if "uses" in step
    ] == ["actions/checkout", "jdx/mise-action"]
    assert client_platforms["steps"][0]["with"]["persist-credentials"] == "false"
    assert client_platforms["steps"][1]["with"] == {"install_args": "python uv age"}
    assert [step["run"] for step in client_platforms["steps"] if "run" in step] == [
        "mise x -- uv run --locked --all-packages --group dev "
        "python -m pytest -q "
        "packages/riverhog-provenance/tests/test_platform_live.py "
        "utilities/gogurt/tests "
        "tests/platform/test_end_user_artifacts.py",
        "mise x -- uv run --locked --all-packages --group dev "
        "python scripts/qualify_installation.py --version 1.0.0",
    ]
    assert "secrets." not in text


def test_release_qualification_reuses_ci_and_publishes_only_sha_bound_summaries() -> None:
    text = QUALIFICATION_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    assert workflow["on"]["schedule"] == [{"cron": "17 7 1,15 * *"}]
    assert workflow["permissions"] == {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "deployments": "read",
    }
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert set(workflow["jobs"]) == {"resolve", "ci", "release-audit"}
    assert workflow["jobs"]["ci"] == {
        "name": "required checks",
        "needs": "resolve",
        "uses": "./.github/workflows/ci.yml",
        "with": {"ref": "${{ needs.resolve.outputs.sha }}"},
        "permissions": {"contents": "read"},
    }
    audit = workflow["jobs"]["release-audit"]
    assert "environment" not in audit
    assert audit["needs"] == ["resolve", "ci"]
    assert audit["env"]["SOURCE_SHA"] == "${{ needs.resolve.outputs.sha }}"
    assert audit["env"]["SOURCE_REF"] == "${{ needs.resolve.outputs.ref }}"
    assert audit["env"]["RIVERHOG_RELEASE_GHA_CACHE"] == "true"
    locate_evidence = next(
        step
        for step in audit["steps"]
        if step["name"] == "Locate qualification evidence outside the source tree"
    )
    assert 'qualification_dir="$RUNNER_TEMP/release-qualification"' in locate_evidence["run"]
    assert '>> "$GITHUB_ENV"' in locate_evidence["run"]
    assert "OPERATIONS_SUMMARY" in locate_evidence["run"]
    assert "OPERATIONS_TIMINGS" in locate_evidence["run"]
    lifecycle_evidence = next(
        step
        for step in audit["steps"]
        if step["name"] == "Exercise disposable operation lifecycles and record timings"
    )
    assert "test_operation_lifecycle_api.py" in lifecycle_evidence["run"]
    assert "test_munchy_server_contract.py" in lifecycle_evidence["run"]
    assert "test_jeb_health.py" in lifecycle_evidence["run"]
    assert "test_collection_reads.py" in lifecycle_evidence["run"]
    assert "test_list_jobs_does_not_scan_all_job_states" in lifecycle_evidence["run"]
    assert "test_source_registry_lists_compact_filtered_pages" in lifecycle_evidence["run"]
    assert "tests.operation_observer" in lifecycle_evidence["run"]
    operation_evidence = next(
        step
        for step in audit["steps"]
        if step["name"] == "Verify and record the complete operation matrix"
    )
    assert "make operation-qualification" in operation_evidence["run"]
    assert "--source-sha $SOURCE_SHA" in operation_evidence["run"]
    assert "--timings $OPERATIONS_TIMINGS" in operation_evidence["run"]
    verify_operations = next(
        step for step in audit["steps"] if step["name"] == "Verify exact-SHA operation evidence"
    )
    assert "riverhog-operation-qualification/v1" in verify_operations["run"]
    assert ".source_sha == $sha" in verify_operations["run"]
    assert "positive_local_lifecycles.status" in verify_operations["run"]
    assert "cli_human_json_projection.status" in verify_operations["run"]
    assert "bounded_state_access.status" in verify_operations["run"]
    assert "event_cursor_restart_resume.status" in verify_operations["run"]
    resolve_source = next(
        step
        for step in workflow["jobs"]["resolve"]["steps"]
        if step["name"] == "Resolve the selected ref once"
    )
    assert resolve_source["env"]["WORKFLOW_REF"] == "${{ github.ref }}"
    assert '[[ "$WORKFLOW_REF" != refs/heads/main ]]' in resolve_source["run"]
    audit_checkout = next(
        step for step in audit["steps"] if step["name"] == "Check out workflow authority"
    )
    assert audit_checkout["with"] == {
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    exact_checkout = next(
        step for step in audit["steps"] if step["name"] == "Check out verified exact source"
    )
    assert exact_checkout["run"] == (
        'git fetch --force --no-tags origin "$SOURCE_SHA"\n'
        'git checkout --detach "$SOURCE_SHA"\n'
        'test "$(git rev-parse --verify HEAD)" = "$SOURCE_SHA"\n'
    )
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
        for step in workflow["jobs"]["resolve"]["steps"] + audit["steps"]
        if "uses" in step
    )
    upload = next(step for step in audit["steps"] if step["name"] == "Upload SHA-bound summaries")
    record = next(
        step for step in audit["steps"] if step["name"] == "Record the completed qualification"
    )
    assert '> "$QUALIFICATION_DIR/qualification.json"' in record["run"]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["path"] == "${{ runner.temp }}/release-qualification/*.json"
    assert "published == false" in text
    assert "riverhog-release-qualification/v1" in text
    assert 'operation_matrix: "passed"' in text
    assert "Analyze (actions)" in text and "Analyze (python)" in text
    assert "release/v1" in text
    assert "v1\\.[0-9]+\\.[0-9]+" in text


def test_release_required_check_names_are_derived_from_stable_job_names() -> None:
    workflow = yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    release = tomllib.loads((REPO_ROOT / "release.toml").read_text(encoding="utf-8"))
    repository = workflow["jobs"]["repository"]
    images = workflow["jobs"]["images"]
    client_platforms = workflow["jobs"]["client-platforms"]
    actual = {
        *(
            repository["name"].replace("${{ matrix.target }}", entry["target"])
            for entry in repository["strategy"]["matrix"]["include"]
        ),
        *(
            images["name"].replace("${{ matrix.target }}", target)
            for target in images["strategy"]["matrix"]["target"]
        ),
        *(
            client_platforms["name"].replace("${{ matrix.os }}", os_name)
            for os_name in client_platforms["strategy"]["matrix"]["os"]
        ),
        "Analyze (actions)",
        "Analyze (python)",
    }

    assert release["governance"]["required_checks"] == sorted(actual)


def test_provider_qualification_is_resumable_dummy_only_and_cloudfront_required() -> None:
    text = PROVIDER_QUALIFICATION_WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)

    def references(job: dict[str, object], context: str) -> set[str]:
        rendered = json.dumps(job)
        return set(re.findall(rf"\$\{{\{{ {context}\.([A-Z0-9_]+) \}}\}}", rendered))

    assert workflow["on"]["schedule"] == [{"cron": "23 1,7,13,19 * * *"}]
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]["corpus_profile"]["options"]) == {
        "regular",
        "multipart",
    }
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["options"]) == {
        "auto",
        "start",
        "poll",
        "restart",
    }
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
    }
    assert workflow["concurrency"] == {
        "group": "provider-qualification",
        "cancel-in-progress": "false",
    }
    assert set(workflow["jobs"]) == {"resolve", "provision_aws", "qualify"}
    resolve_job = workflow["jobs"]["resolve"]
    provision_job = workflow["jobs"]["provision_aws"]
    job = workflow["jobs"]["qualify"]
    assert "environment" not in resolve_job and "permissions" not in resolve_job
    assert resolve_job["outputs"] == {
        "action": "${{ steps.mode.outputs.action }}",
        "artifact_id": "${{ steps.mode.outputs.artifact_id }}",
        "profile": "${{ steps.mode.outputs.profile }}",
        "source_ref": "${{ steps.mode.outputs.source_ref }}",
        "source_sha": "${{ steps.mode.outputs.source_sha }}",
    }
    assert references(resolve_job, "secrets") == set()

    assert provision_job["needs"] == "resolve"
    assert "needs.resolve.outputs.action == 'start'" in provision_job["if"]
    assert "needs.resolve.outputs.action == 'restart'" in provision_job["if"]
    assert provision_job["environment"] == "provider-qualification-provisioning"
    assert provision_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert references(provision_job, "vars") == {
        "RIVERHOG_QUALIFICATION_AWS_DEEP_ARCHIVE_BUCKET",
        "RIVERHOG_QUALIFICATION_AWS_PROVISION_ROLE_ARN",
        "RIVERHOG_QUALIFICATION_AWS_REGION",
        "RIVERHOG_QUALIFICATION_CLOUDFRONT_PUBLIC_KEY",
    }
    assert references(provision_job, "secrets") == set()
    assert "RIVERHOG_QUALIFICATION_B2_" not in json.dumps(provision_job)

    assert job["needs"] == ["resolve", "provision_aws"]
    assert "needs.resolve.outputs.action != 'skip'" in job["if"]
    assert "needs.provision_aws.result == 'skipped'" in job["if"]
    assert job["environment"] == "provider-qualification"
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert job["runs-on"] == "ubuntu-24.04"
    assert {name for name in job["env"] if name.endswith("_BUCKET")} == {
        "RIVERHOG_QUALIFICATION_AWS_DEEP_ARCHIVE_BUCKET",
        "RIVERHOG_QUALIFICATION_B2_ARCHIVE_BUCKET",
        "RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_BUCKET",
    }
    assert references(job, "vars") == {
        "RIVERHOG_QUALIFICATION_AWS_DEEP_ARCHIVE_BUCKET",
        "RIVERHOG_QUALIFICATION_AWS_REGION",
        "RIVERHOG_QUALIFICATION_AWS_RUNTIME_ROLE_ARN",
        "RIVERHOG_QUALIFICATION_B2_ARCHIVE_ACCESS_KEY_ID",
        "RIVERHOG_QUALIFICATION_B2_ARCHIVE_BUCKET",
        "RIVERHOG_QUALIFICATION_B2_REGION",
        "RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_ACCESS_KEY_ID",
        "RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_BUCKET",
        "RIVERHOG_QUALIFICATION_B2_S3_ENDPOINT_URL",
        "RIVERHOG_QUALIFICATION_CLOUDFRONT_PUBLIC_KEY",
    }
    assert references(job, "secrets") == {
        "RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE",
        "RIVERHOG_QUALIFICATION_B2_ARCHIVE_SECRET_ACCESS_KEY",
        "RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_SECRET_ACCESS_KEY",
        "RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN",
        "RIVERHOG_QUALIFICATION_CLOUDFRONT_PRIVATE_KEY",
    }
    steps = job["steps"]
    action_steps = [
        step
        for workflow_job in workflow["jobs"].values()
        for step in workflow_job["steps"]
        if "uses" in step
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)

    resolve = next(
        step
        for step in resolve_job["steps"]
        if step["name"] == "Resolve continuation and exact source"
    )
    assert '"$WORKFLOW_REF" != refs/heads/main' in resolve["run"]
    assert "actions/workflows/provider-qualification.yml/runs?branch=main" in resolve["run"]
    assert "actions/runs/$run_id/artifacts" in resolve["run"]
    assert "release/v1" in resolve["run"]
    assert "source_sha" in resolve["run"]
    download = next(
        step for step in steps if step["name"] == "Download verified continuation state"
    )
    assert "needs.resolve.outputs.action == 'poll'" in download["if"]
    assert "needs.resolve.outputs.action == 'restart'" in download["if"]
    assert "actions/artifacts/$ARTIFACT_ID/zip" in download["run"]
    assert ".active == true" in download["run"]
    cleanup = next(step for step in steps if step["name"] == "Clean superseded dummy B2 state")
    assert cleanup["if"] == "needs.resolve.outputs.action == 'restart'"
    assert "cleanup-b2" in cleanup["run"]
    exact = next(step for step in steps if step["name"] == "Check out verified exact source")
    assert 'test "$(git rev-parse --verify HEAD)" = "$SOURCE_SHA"' in exact["run"]

    provision = next(
        step
        for step in provision_job["steps"]
        if step["name"] == "Configure AWS provisioning identity"
    )
    runtime = next(step for step in steps if step["name"] == "Configure AWS runtime identity")
    reconcile = next(
        step
        for step in provision_job["steps"]
        if step["name"] == "Reconcile dedicated AWS infrastructure"
    )
    b2_check = next(
        step for step in steps if step["name"] == "Verify manually provisioned B2 infrastructure"
    )
    assert "AWS_PROVISION_ROLE_ARN" in provision["with"]["role-to-assume"]
    assert "AWS_RUNTIME_ROLE_ARN" in runtime["with"]["role-to-assume"]
    assert runtime["with"]["unset-current-credentials"] == "true"
    assert "env" not in reconcile
    assert set(b2_check["env"]) == {
        "RIVERHOG_QUALIFICATION_B2_ARCHIVE_ACCESS_KEY_ID",
        "RIVERHOG_QUALIFICATION_B2_ARCHIVE_SECRET_ACCESS_KEY",
        "RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_ACCESS_KEY_ID",
        "RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_SECRET_ACCESS_KEY",
    }
    assert b2_check["run"] == 'make provider-qualification args="b2-check $CONFIG_PATH"'

    state = next(step for step in steps if step["name"] == "Create and verify deterministic state")
    deployment_env = next(
        step for step in steps if step["name"] == "Generate the disposable deployment environment"
    )
    deployment = next(
        step for step in steps if step["name"] == "Start the disposable Riverhog deployment"
    )
    snapshot = next(
        step for step in steps if step["name"] == "Snapshot bounded disposable database state"
    )
    assert "corpus-create" in state["run"] and "checkpoint-start" in state["run"]
    assert 'test -z "${RIVERHOG_DATABASE_URL:-}"' in deployment_env["run"]
    assert "RIVERHOG_DATABASE_URL=" in deployment_env["run"]
    assert "docker volume ls" in deployment_env["run"]
    assert "postgresql+psycopg://riverhog:riverhog@postgres:5432/riverhog" in deployment["run"]
    assert "logs --no-color --tail 80 app state" in deployment["run"]
    assert "default_container; default_container()" in deployment["run"]
    assert "timeout 30s" in deployment["run"]
    assert "pg_dump" in snapshot["run"]
    assert "536870912" in snapshot["run"]

    package = next(
        step
        for step in steps
        if step["name"] == "Package resumable dummy state and public evidence"
    )
    assert package["if"] == (
        "always() && env.STATE_DIR != '' && steps.deployment.outcome == 'success'"
    )
    upload = next(step for step in steps if step["name"] == "Upload bounded qualification state")
    assert 'if [[ "$phase" == cleaned || "$phase" == failed ]]' in package["run"]
    assert 'cp "$STATE_DIR/checkpoint.json" "$STATE_DIR/database.dump"' in package["run"]
    assert "retention_days=90" in package["run"]
    assert upload["with"] == {
        "name": "provider-qualification-state",
        "path": "${{ runner.temp }}/provider-public-artifact",
        "if-no-files-found": "error",
        "retention-days": "${{ steps.package.outputs.retention_days }}",
    }
    assert "age --encrypt" not in text and "age --decrypt" not in text
    assert "RIVERHOG_QUALIFICATION_CLOUDFRONT_PRIVATE_KEY" in text
    assert not any(
        name.endswith("SECRET_ACCESS_KEY")
        or name
        in {
            "RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE",
            "RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN",
        }
        for name in job["env"]
    )


def test_client_platform_toolchain_is_locked_for_every_matrix_os() -> None:
    lock = tomllib.loads(MISE_LOCK.read_text(encoding="utf-8"))

    for tool in ("python", "uv"):
        entries = lock["tools"][tool]
        assert len(entries) == 1
        platforms = {
            key.removeprefix("platforms."): value
            for key, value in entries[0].items()
            if key.startswith("platforms.")
        }
        assert set(platforms) == {"linux-x64", "macos-arm64", "windows-x64"}
        windows = platforms["windows-x64"]
        assert windows["url"].startswith("https://github.com/")
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", windows["checksum"])
