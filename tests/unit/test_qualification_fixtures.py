from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from gogurt_core.core import execute_gogurt_action, load_gogurt_actions, plan_gogurt_action
from riverhog_ftp_adapter.config import load_config as load_adapter_config
from stove0_core import RecipeCatalog

from tests.gogurt_provider import path_mounted_volume_provider

REPO_ROOT = Path(__file__).resolve().parents[2]
QUALIFICATION_INPUTS = {
    REPO_ROOT / "qualification/fixtures/gogurt/gogurt-routes.yaml",
    REPO_ROOT / "qualification/fixtures/gogurt/scripts/fake_archive_device.py",
    REPO_ROOT / "qualification/fixtures/riverhog-ftp-adapter/config.json",
    REPO_ROOT / "qualification/fixtures/stove0/recipes.yaml",
    REPO_ROOT / "qualification/provider/config.toml",
}


def test_every_checked_qualification_input_runs_through_its_real_consumer(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    checked_inputs = {
        path
        for path in (REPO_ROOT / "qualification").rglob("*")
        if path.is_file() and path.suffix != ".md" and "__pycache__" not in path.parts
    }
    assert checked_inputs == QUALIFICATION_INPUTS

    gogurt_root = REPO_ROOT / "qualification/fixtures/gogurt"
    actions = load_gogurt_actions(gogurt_root / "gogurt-routes.yaml")
    assert [action.route for action in actions] == ["example-camera-card"]

    mounted_device = tmp_path / "mounted-device"
    mounted_device.mkdir()
    (mounted_device / ".gogurt").write_text("example-camera-card\n", encoding="utf-8")
    provider = path_mounted_volume_provider(lambda: [mounted_device])
    action_plan = plan_gogurt_action(
        gogurt_root / "gogurt-routes.yaml",
        mounted_device,
        provider=provider,
    )
    completed = execute_gogurt_action(action_plan, provider=provider, capture_output=True)
    assert completed.returncode == 0
    assert "archive example-camera" in completed.stdout

    recipes = RecipeCatalog.load(REPO_ROOT / "qualification/fixtures/stove0/recipes.yaml")
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
        REPO_ROOT / "qualification/fixtures/riverhog-ftp-adapter/config.json"
    )
    assert [source.id for source in adapters.sources] == ["ftp-intake"]

    script = REPO_ROOT / "scripts/provider_qualification.py"
    spec = importlib.util.spec_from_file_location("provider_qualification_config", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    qualification = module.load_config(REPO_ROOT / "qualification/provider/config.toml")
    assert qualification.cloudfront.enabled is True
    assert {bucket.logical_name for bucket in qualification.buckets} == {
        "b2-archive",
        "b2-retrieval-cache",
        "aws-deep-archive",
    }
