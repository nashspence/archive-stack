from __future__ import annotations

import importlib.util
import random
import sys
import tempfile
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

    def test_run_qcut_video_uses_supplied_review_clip_plan(self) -> None:
        class StopPlanning(RuntimeError):
            pass

        captured: dict[str, int] = {}
        original_iter_files = av1.iter_files
        original_plan_review_clips = av1.plan_review_clips
        try:
            av1.iter_files = lambda input_dir, extensions: [Path("/data/input/a.mp4")]

            def fake_plan_review_clips(
                sources: list[Path],
                *,
                target_sec: int,
                min_sec: int,
                max_sec: int,
                seed: str | None = None,
            ) -> dict[str, object]:
                captured.update(
                    {
                        "target_sec": target_sec,
                        "min_sec": min_sec,
                        "max_sec": max_sec,
                    }
                )
                raise StopPlanning

            av1.plan_review_clips = fake_plan_review_clips
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(StopPlanning):
                    av1.run_qcut_video(
                        Path(tmp) / "input",
                        Path(tmp) / "review",
                        archive=None,
                        dry_run=True,
                        clip_plan=av1.ReviewClipPlanConfig(
                            target_seconds=90,
                            min_seconds=4,
                            max_seconds=7,
                        ),
                    )
        finally:
            av1.iter_files = original_iter_files
            av1.plan_review_clips = original_plan_review_clips

        self.assertEqual(captured, {"target_sec": 90, "min_sec": 4, "max_sec": 7})

    def test_run_qcut_video_uses_job_concurrency_cap(self) -> None:
        captured_workers: list[int] = []

        class ImmediateFuture:
            def __init__(self, value: dict[str, object]) -> None:
                self.value = value

            def result(self) -> dict[str, object]:
                return self.value

        class CapturingExecutor:
            def __init__(self, *, max_workers: int) -> None:
                captured_workers.append(max_workers)

            def __enter__(self) -> "CapturingExecutor":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def submit(self, fn: object, clip: dict[str, object]) -> ImmediateFuture:
                return ImmediateFuture(fn(clip))  # type: ignore[operator]

        original_max_parallel_encodes = av1.MAX_PARALLEL_ENCODES
        original_thread_pool_executor = av1.ThreadPoolExecutor
        original_as_completed = av1.as_completed
        original_iter_files = av1.iter_files
        original_plan_review_clips = av1.plan_review_clips
        original_qcut_video_command = av1.qcut_video_command
        try:
            av1.MAX_PARALLEL_ENCODES = 9
            av1.ThreadPoolExecutor = CapturingExecutor
            av1.as_completed = lambda futures: list(futures)
            av1.iter_files = lambda input_dir, extensions: [Path(input_dir) / "a.mp4"]

            def fake_plan_review_clips(
                sources: list[Path],
                *,
                target_sec: int,
                min_sec: int,
                max_sec: int,
                seed: str | None = None,
            ) -> dict[str, object]:
                source = sources[0]
                return {
                    "clips": [
                        {
                            "index": 0,
                            "source": str(source),
                            "start": 0.0,
                            "length": 6.0,
                            "epoch": 1_800_000_000,
                        }
                    ],
                    "files": [
                        {
                            "path": str(source),
                            "duration": 6.0,
                            "base_epoch": 1_800_000_000,
                            "quota": 1,
                        }
                    ],
                }

            av1.plan_review_clips = fake_plan_review_clips
            av1.qcut_video_command = lambda *args, **kwargs: [sys.executable, "-c", ""]

            with tempfile.TemporaryDirectory() as tmp:
                av1.run_qcut_video(
                    Path(tmp) / "input",
                    Path(tmp) / "review",
                    archive=av1.ArchiveEncodeProfile(),
                    dry_run=True,
                    max_parallel_encodes=2,
                )
        finally:
            av1.MAX_PARALLEL_ENCODES = original_max_parallel_encodes
            av1.ThreadPoolExecutor = original_thread_pool_executor
            av1.as_completed = original_as_completed
            av1.iter_files = original_iter_files
            av1.plan_review_clips = original_plan_review_clips
            av1.qcut_video_command = original_qcut_video_command

        self.assertEqual(captured_workers, [2])

    def test_job_concurrency_cap_cannot_exceed_target_limit(self) -> None:
        original_max_parallel_encodes = av1.MAX_PARALLEL_ENCODES
        try:
            av1.MAX_PARALLEL_ENCODES = 4

            self.assertEqual(av1.resolve_max_parallel_encodes(None), 4)
            self.assertEqual(av1.resolve_max_parallel_encodes(2), 2)
            self.assertEqual(av1.resolve_max_parallel_encodes(8), 4)
        finally:
            av1.MAX_PARALLEL_ENCODES = original_max_parallel_encodes

    def test_qcut_video_command_uses_cuda_format_filter_without_timestamp_overlay(self) -> None:
        original_video_scale_mode = av1.VIDEO_SCALE_MODE
        original_archive_decoder_args = av1.archive_decoder_args
        original_archive_scale_target = av1.archive_scale_target
        original_archive_frame_rate_filters = av1.archive_frame_rate_filters
        original_ffmpeg_filter_available = av1.ffmpeg_filter_available
        try:
            av1.VIDEO_SCALE_MODE = "cuda"
            av1.archive_decoder_args = lambda source: ["-c:v", "h264_cuvid"]
            av1.archive_scale_target = lambda source, archive: None
            av1.archive_frame_rate_filters = lambda source, archive: []
            av1.ffmpeg_filter_available = lambda name: name == "scale_cuda"

            with tempfile.TemporaryDirectory() as tmp:
                cmd = av1.qcut_video_command(
                    Path("/data/input/source.mp4"),
                    Path(tmp) / "output" / "review.webm",
                    start=1.25,
                    length=6.0,
                    archive=av1.ArchiveEncodeProfile(pix_fmt="p010le", scale_flags="lanczos"),
                )
        finally:
            av1.VIDEO_SCALE_MODE = original_video_scale_mode
            av1.archive_decoder_args = original_archive_decoder_args
            av1.archive_scale_target = original_archive_scale_target
            av1.archive_frame_rate_filters = original_archive_frame_rate_filters
            av1.ffmpeg_filter_available = original_ffmpeg_filter_available

        rendered = " ".join(cmd)
        self.assertNotIn("drawtext", rendered)
        self.assertIn("-hwaccel", cmd)
        self.assertIn("-hwaccel_output_format", cmd)
        self.assertIn("scale_cuda=format=p010le", cmd)
        self.assertNotIn("-pix_fmt", cmd)

    def test_run_command_keeps_only_output_tails(self) -> None:
        result = av1.run_command(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(b'a' * 5001); "
                    "sys.stderr.buffer.write(b'b' * 5002)"
                ),
            ],
            action="tail test",
        )

        self.assertEqual(result["stdout"], "a" * 4000)
        self.assertEqual(result["stderr"], "b" * 4000)

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
