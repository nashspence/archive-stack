from __future__ import annotations

from pathlib import Path

from gogurt.core import execute_gogurt_action, load_gogurt_actions, plan_gogurt_action
from mango_fish.relay import load_config as load_mango_fish_config
from munchy_workflows.job_authoring import (
    build_review_sweep_plan,
    load_munchy_job_config,
    munchy_job_defaults_from_config,
)
from munchy_workflows.profiles import load_encode_profile

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_FILES = {
    REPO_ROOT / "utilities/mango-fish/config/mango-fish.yaml",
    REPO_ROOT / "companions/munchy/config/examples/av1-nvenc-profile.yaml",
    REPO_ROOT / "companions/munchy/config/examples/job.yaml",
    REPO_ROOT / "companions/munchy/config/examples/review-sweep-job.yaml",
    REPO_ROOT / "utilities/gogurt/config/examples/gogurt-routes.yaml",
    REPO_ROOT / "utilities/gogurt/config/examples/scripts/fake_archive_device.py",
}


def test_every_checked_example_runs_through_its_real_consumer(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    checked_files = {
        path
        for root in (
            REPO_ROOT / "utilities/mango-fish/config",
            REPO_ROOT / "companions/munchy/config/examples",
            REPO_ROOT / "utilities/gogurt/config/examples",
        )
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert checked_files == EXAMPLE_FILES

    gogurt_root = REPO_ROOT / "utilities/gogurt/config/examples"
    actions = load_gogurt_actions(gogurt_root / "gogurt-routes.yaml")
    assert [action.route for action in actions] == ["example-camera-card"]

    mounted_device = tmp_path / "mounted-device"
    mounted_device.mkdir()
    (mounted_device / ".gogurt").write_text("example-camera-card\n", encoding="utf-8")
    action_plan = plan_gogurt_action(gogurt_root / "gogurt-routes.yaml", mounted_device)
    completed = execute_gogurt_action(action_plan, capture_output=True)
    assert completed.returncode == 0
    assert "archive example-camera" in completed.stdout

    for name in (
        "RIVERHOG_EVENT_TOKEN",
        "MUNCHY_EVENT_TOKEN",
        "JEB_EVENT_TOKEN",
        "MANGO_FISH_WEBHOOK_URL",
    ):
        monkeypatch.setenv(name, "fake-example-value")
    mango_fish = load_mango_fish_config(REPO_ROOT / "utilities/mango-fish/config/mango-fish.yaml")
    assert [source.name for source in mango_fish.sources] == ["riverhog", "munchy", "jeb"]

    munchy_examples = REPO_ROOT / "companions/munchy/config/examples"
    profile = load_encode_profile(munchy_examples / "av1-nvenc-profile.yaml")
    assert profile.name == "example-camera"

    job_config = load_munchy_job_config(munchy_examples / "job.yaml")
    job_defaults = munchy_job_defaults_from_config(job_config)
    assert set(job_defaults["groups"]) == {"video", "preserve"}

    source = tmp_path / "review-source"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"example")
    review_config = munchy_examples / "review-sweep-job.yaml"
    review_defaults = munchy_job_defaults_from_config(load_munchy_job_config(review_config))
    assert review_defaults["workflow_mode"] == "review"
    review_plan = build_review_sweep_plan(
        source=source,
        template_id="example-camera-review-sweep",
        config_path=review_config,
    )
    assert review_plan["ok"] is True
    assert review_plan["variants_total"] == 8
    assert str(review_plan["routes"][0]["variants"][0]["location"]).startswith("review-remote:")
