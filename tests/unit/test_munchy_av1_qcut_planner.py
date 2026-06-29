from __future__ import annotations

import importlib.util
import random
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "services" / "munchy-av1-nvenc" / "app" / "main.py"
)
SPEC = importlib.util.spec_from_file_location("munchy_av1_main", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {MODULE_PATH}")
av1 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = av1
SPEC.loader.exec_module(av1)


class QcutPlannerTests(unittest.TestCase):
    def test_many_sources_are_bounded_to_available_clip_slots(self) -> None:
        quotas = av1.quotas_by_duration([30.0] * 1000, slot_count=80, min_seconds=6)

        self.assertEqual(sum(quotas), 80)
        self.assertEqual(sum(1 for quota in quotas if quota > 0), 80)
        self.assertEqual(quotas[0], 1)
        self.assertEqual(quotas[-1], 1)

    def test_small_collections_still_cover_every_eligible_source(self) -> None:
        quotas = av1.quotas_by_duration([30.0, 60.0, 90.0], slot_count=8, min_seconds=6)

        self.assertEqual(sum(quotas), 8)
        self.assertTrue(all(quota >= 1 for quota in quotas))
        self.assertGreaterEqual(quotas[2], quotas[1])
        self.assertGreaterEqual(quotas[1], quotas[0])

    def test_short_sources_can_still_be_sampled_when_all_are_short(self) -> None:
        quotas = av1.quotas_by_duration([2.0, 2.0, 2.0], slot_count=2, min_seconds=6)

        self.assertEqual(sum(quotas), 2)
        self.assertEqual(quotas[0], 1)
        self.assertEqual(quotas[-1], 1)

    def test_plan_review_clips_keeps_large_card_reviews_bounded(self) -> None:
        sources = [Path(f"/data/input/source{i:04d}.mp4") for i in range(1000)]
        original_duration = av1.ffprobe_duration
        original_base_epoch = av1.base_epoch_for_file
        try:
            av1.ffprobe_duration = lambda path: 30.0
            av1.base_epoch_for_file = lambda path: 1_800_000_000 + sources.index(path) * 30
            random.seed(1234)

            plan = av1.plan_review_clips(sources, target_sec=600, min_sec=6, max_sec=9)
        finally:
            av1.ffprobe_duration = original_duration
            av1.base_epoch_for_file = original_base_epoch

        self.assertEqual(len(plan["clips"]), len(plan["slots"]))
        self.assertLessEqual(len(plan["clips"]), 100)
        self.assertEqual(plan["files"][0]["quota"], 1)
        self.assertEqual(plan["files"][-1]["quota"], 1)

    def test_plan_review_clips_is_stable_for_same_collection(self) -> None:
        sources = [Path(f"/data/input/source{i:02d}.mp4") for i in range(8)]
        original_duration = av1.ffprobe_duration
        original_base_epoch = av1.base_epoch_for_file
        try:
            av1.ffprobe_duration = lambda path: 45.0
            av1.base_epoch_for_file = lambda path: 1_800_000_000 + sources.index(path) * 45
            random.seed(1)
            first = av1.plan_review_clips(
                sources,
                target_sec=120,
                min_sec=6,
                max_sec=9,
            )
            random.seed(999)
            second = av1.plan_review_clips(
                sources,
                target_sec=120,
                min_sec=6,
                max_sec=9,
            )
        finally:
            av1.ffprobe_duration = original_duration
            av1.base_epoch_for_file = original_base_epoch

        self.assertEqual(first["seed"], second["seed"])
        self.assertEqual(first["slots"], second["slots"])
        self.assertEqual(first["clips"], second["clips"])

    def test_clip_progress_payload_reports_review_clip_progress(self) -> None:
        payload = av1.clip_progress_payload(
            task="qcut_video",
            phase="encoding_clips",
            clips_total=10,
            clips_done=3,
            clips_running=2,
            clips_failed=1,
            output_bytes=1024,
            active_output_bytes=512,
            started_at="2026-06-05T00:00:00Z",
        )

        self.assertEqual(payload["mode"], "qcut_video")
        self.assertEqual(payload["task"], "qcut_video")
        self.assertEqual(payload["phase"], "encoding_clips")
        self.assertEqual(payload["clips_total"], 10)
        self.assertEqual(payload["clips_done"], 3)
        self.assertEqual(payload["clips_running"], 2)
        self.assertEqual(payload["clips_failed"], 1)
        self.assertEqual(payload["percent_clips"], 30.0)
        self.assertEqual(payload["output_bytes"], 1024)
        self.assertEqual(payload["active_output_bytes"], 512)
        self.assertFalse(payload["completed"])


if __name__ == "__main__":
    unittest.main()
