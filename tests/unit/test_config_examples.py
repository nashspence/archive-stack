from __future__ import annotations

import subprocess
from pathlib import Path

from gogurt.core import load_gogurt_actions
from jeb.collector import config_from_env
from munchy.job_authoring import (
    build_review_sweep_plan,
    load_munchy_job_config,
    munchy_job_defaults_from_config,
)
from munchy.profiles import load_encode_profile

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPO_ROOT / "config" / "examples"
EXAMPLE_FILES = {
    Path("gogurt/gogurt-routes.yaml"),
    Path("gogurt/scripts/fake-archive-device"),
    Path("jeb/jeb.env"),
    Path("munchy/av1-nvenc-profile.yaml"),
    Path("munchy/job.yaml"),
    Path("munchy/review-sweep-job.yaml"),
}


def _parse_env_example(path: Path) -> dict[str, str]:
    lines = (
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    return dict(line.split("=", 1) for line in lines)


def test_every_checked_example_runs_through_its_real_consumer(tmp_path: Path) -> None:
    checked_files = {
        path.relative_to(EXAMPLE_ROOT)
        for path in EXAMPLE_ROOT.rglob("*")
        if path.is_file()
    }
    assert checked_files == EXAMPLE_FILES

    actions = load_gogurt_actions(EXAMPLE_ROOT / "gogurt/gogurt-routes.yaml")
    assert [action.route for action in actions] == ["example-camera-card"]

    script = EXAMPLE_ROOT / "gogurt/scripts/fake-archive-device"
    completed = subprocess.run(
        [str(script), str(tmp_path / "mounted-device"), "example-camera"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "archive example-camera" in completed.stdout

    jeb_config = config_from_env(_parse_env_example(EXAMPLE_ROOT / "jeb/jeb.env"))
    assert [account.id for account in jeb_config.accounts] == [
        "example-camera",
        "example-phone",
    ]

    profile = load_encode_profile(EXAMPLE_ROOT / "munchy/av1-nvenc-profile.yaml")
    assert profile.name == "example-camera"

    job_config = load_munchy_job_config(EXAMPLE_ROOT / "munchy/job.yaml")
    job_defaults = munchy_job_defaults_from_config(job_config)
    assert set(job_defaults["groups"]) == {"video", "preserve"}

    source = tmp_path / "review-source"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"example")
    review_config = EXAMPLE_ROOT / "munchy/review-sweep-job.yaml"
    review_defaults = munchy_job_defaults_from_config(load_munchy_job_config(review_config))
    assert review_defaults["workflow_mode"] == "review"
    review_plan = build_review_sweep_plan(source=source, config_path=review_config)
    assert review_plan["ok"] is True
    assert review_plan["variants_total"] == 8
    assert str(review_plan["routes"][0]["variants"][0]["destination"]).startswith(
        "review-remote:"
    )
