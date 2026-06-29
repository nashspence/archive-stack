from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "services" / "munchy-av1-nvenc" / "app" / "main.py"
)
SPEC = importlib.util.spec_from_file_location("munchy_av1_main", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {MODULE_PATH}")
av1 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = av1
SPEC.loader.exec_module(av1)


class ContainerProfileTests(unittest.TestCase):
    def test_webm_archive_command_uses_webm_muxer_and_video_audio_only_maps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "clip.webm"
            source.write_bytes(b"source")

            archive = av1.ArchiveEncodeProfile(container="webm", quality=49)
            with (
                patch.object(av1, "validate_archive_container_source") as validate_source,
                patch.object(av1, "archive_decoder_args", return_value=[]),
                patch.object(av1, "archive_video_filters", return_value=[]),
            ):
                cmd = av1.av1_archive_command(source, output, archive)

        validate_source.assert_called_once_with(source, archive)
        self.assertEqual(av1.archive_container_suffix(archive), ".webm")
        self.assertEqual(cmd[cmd.index("-f") + 1], "webm")
        self.assertIn("0:v?", cmd)
        self.assertIn("0:a?", cmd)
        self.assertNotIn("0:s?", cmd)
        self.assertNotIn("0:t?", cmd)
        self.assertNotIn("-c:s", cmd)
        self.assertNotIn("-c:t", cmd)

    def test_mkv_archive_command_keeps_side_stream_maps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "clip.mkv"
            source.write_bytes(b"source")

            archive = av1.ArchiveEncodeProfile(container="mkv", quality=49)
            with (
                patch.object(av1, "archive_decoder_args", return_value=[]),
                patch.object(av1, "archive_video_filters", return_value=[]),
            ):
                cmd = av1.av1_archive_command(source, output, archive)

        self.assertEqual(av1.archive_container_suffix(archive), ".mkv")
        self.assertEqual(cmd[cmd.index("-f") + 1], "matroska")
        self.assertIn("0:s?", cmd)
        self.assertIn("0:t?", cmd)
        self.assertIn("-c:s", cmd)
        self.assertIn("-c:t", cmd)

    def test_archive_command_uses_cuda_lanczos_scale_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "clip.webm"
            source.write_bytes(b"source")

            archive = av1.ArchiveEncodeProfile(
                container="webm",
                max_height=720,
                scale_flags="lanczos",
                pix_fmt="p010le",
            )
            scale_details = {
                "crop_top": 0,
                "crop_bottom": 0,
                "crop_left": 0,
                "crop_right": 0,
                "rotation": 0,
            }
            with (
                patch.object(av1, "VIDEO_SCALE_MODE", "cuda"),
                patch.object(av1, "validate_archive_container_source"),
                patch.object(av1, "archive_decoder_args", return_value=["-c:v", "hevc_cuvid"]),
                patch.object(
                    av1,
                    "archive_scale_target",
                    return_value=(scale_details, (1280, 720)),
                ),
                patch.object(av1, "archive_frame_rate_filters", return_value=[]),
                patch.object(av1, "ffmpeg_filter_available", return_value=True),
            ):
                cmd = av1.av1_archive_command(source, output, archive)

        self.assertIn("-vf", cmd)
        self.assertEqual(
            cmd[cmd.index("-vf") + 1],
            "scale_cuda=w=1280:h=720:format=p010le:interp_algo=lanczos",
        )
        self.assertIn("-hwaccel", cmd)
        self.assertIn("-hwaccel_output_format", cmd)
        self.assertNotIn("-pix_fmt", cmd)

    def test_cuda_scale_mode_falls_back_without_cuvid_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "clip.webm"
            source.write_bytes(b"source")

            archive = av1.ArchiveEncodeProfile(
                container="webm",
                max_height=720,
                scale_flags="lanczos",
            )
            scale_details = {
                "crop_top": 0,
                "crop_bottom": 0,
                "crop_left": 0,
                "crop_right": 0,
                "rotation": 0,
            }
            with (
                patch.object(av1, "VIDEO_SCALE_MODE", "cuda"),
                patch.object(av1, "validate_archive_container_source"),
                patch.object(av1, "archive_decoder_args", return_value=[]),
                patch.object(
                    av1,
                    "archive_scale_target",
                    return_value=(scale_details, (1280, 720)),
                ),
                patch.object(av1, "archive_frame_rate_filters", return_value=[]),
                patch.object(
                    av1,
                    "archive_video_filters",
                    return_value=["scale=-2:720:flags=lanczos"],
                ),
                patch.object(av1, "ffmpeg_filter_available", return_value=True),
            ):
                cmd = av1.av1_archive_command(source, output, archive)

        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=-2:720:flags=lanczos")

    def test_webm_container_allows_iso_bmff_data_streams_for_source_artifacts(self) -> None:
        archive = av1.ArchiveEncodeProfile(container="webm")
        with patch.object(
            av1,
            "ffprobe_json",
            return_value={
                "streams": [
                    {"index": 0, "codec_type": "video", "disposition": {}},
                    {"index": 1, "codec_type": "data", "disposition": {}},
                ],
                "chapters": [],
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                "programs": [],
            },
        ):
            av1.validate_archive_container_source(Path("clip.mp4"), archive)

    def test_webm_container_rejects_matroska_data_streams_that_mkv_can_preserve(self) -> None:
        archive = av1.ArchiveEncodeProfile(container="webm")
        with patch.object(
            av1,
            "ffprobe_json",
            return_value={
                "streams": [
                    {"index": 0, "codec_type": "video", "disposition": {}},
                    {"index": 1, "codec_type": "data", "disposition": {}},
                ],
                "chapters": [],
                "format": {"format_name": "matroska,webm"},
                "programs": [],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "streams that need MKV: stream:1"):
                av1.validate_archive_container_source(Path("clip.mkv"), archive)

    def test_webm_container_rejects_chapters(self) -> None:
        archive = av1.ArchiveEncodeProfile(container="webm")
        with patch.object(
            av1,
            "ffprobe_json",
            return_value={
                "streams": [{"index": 0, "codec_type": "video", "disposition": {}}],
                "chapters": [{"id": 0}],
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                "programs": [],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "sources with chapters"):
                av1.validate_archive_container_source(Path("clip.mp4"), archive)


if __name__ == "__main__":
    unittest.main()
