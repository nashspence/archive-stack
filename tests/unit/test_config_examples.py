from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from gogurt.core import execute_gogurt_action, load_gogurt_actions, plan_gogurt_action
from mango_fish.relay import load_config as load_mango_fish_config
from riverhog_ftp_adapter.config import load_config as load_adapter_config
from stove0_core import RecipeCatalog

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_FILES = {
    REPO_ROOT / "utilities/mango-fish/config/mango-fish.yaml",
    REPO_ROOT / "companions/stove0/config/recipes.example.yaml",
    REPO_ROOT / "reference/riverhog/ingress/ftp/config/ftp-adapter.example.json",
    REPO_ROOT / "utilities/gogurt/config/examples/gogurt-routes.yaml",
    REPO_ROOT / "utilities/gogurt/config/examples/scripts/fake_archive_device.py",
    REPO_ROOT / "config/provider-qualification.example.toml",
}


def test_every_checked_example_runs_through_its_real_consumer(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    checked_files = {
        path
        for root in (
            REPO_ROOT / "utilities/mango-fish/config",
            REPO_ROOT / "companions/stove0/config",
            REPO_ROOT / "reference/riverhog/ingress/ftp/config",
            REPO_ROOT / "utilities/gogurt/config/examples",
            REPO_ROOT / "config",
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
        "STOVE0_EVENT_TOKEN",
        "MANGO_FISH_WEBHOOK_URL",
    ):
        monkeypatch.setenv(name, "fake-example-value")
    mango_fish = load_mango_fish_config(REPO_ROOT / "utilities/mango-fish/config/mango-fish.yaml")
    assert [source.name for source in mango_fish.sources] == ["riverhog", "stove0"]

    recipes = RecipeCatalog.load(REPO_ROOT / "companions/stove0/config/recipes.example.yaml")
    assert {recipe.id for recipe in recipes.recipes} == {
        "stove0.audio-archive/v1",
        "stove0.conformance-media/v1",
        "stove0.review-effect/v1",
        "stove0.review/v1",
        "stove0.video-archive/v1",
    }

    monkeypatch.setenv("RIVERHOG_TOKEN", "fake-riverhog-token")
    monkeypatch.setenv("RIVERHOG_FTP_ADAPTER_API_TOKEN", "fake-adapter-token")
    adapters = load_adapter_config(
        REPO_ROOT / "reference/riverhog/ingress/ftp/config/ftp-adapter.example.json"
    )
    assert [source.id for source in adapters.sources] == ["ftp-intake"]

    script = REPO_ROOT / "scripts/provider_qualification.py"
    spec = importlib.util.spec_from_file_location("provider_qualification_example", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    qualification = module.load_config(REPO_ROOT / "config/provider-qualification.example.toml")
    assert qualification.cloudfront.enabled is True
    assert {bucket.logical_name for bucket in qualification.buckets} == {
        "b2-archive",
        "b2-retrieval-cache",
        "aws-deep-archive",
    }
