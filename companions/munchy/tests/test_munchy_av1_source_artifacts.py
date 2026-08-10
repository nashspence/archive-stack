from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from munchy_av1_nvenc import main as av1


class SourceArtifactsTests(unittest.TestCase):
    def test_status_path_uses_an_opaque_storage_segment(self) -> None:
        job_id = "../../operator-visible-job"
        expected_segment = hashlib.sha256(job_id.encode("utf-8")).hexdigest()

        with tempfile.TemporaryDirectory() as tmp, patch.object(av1, "DATA_DIR", Path(tmp)):
            self.assertEqual(
                av1.status_path(job_id),
                Path(tmp) / "jobs" / expected_segment / "status.json",
            )

    def test_target_paths_resolve_under_the_configured_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (root / "input").symlink_to(outside, target_is_directory=True)
            with patch.object(av1, "DATA_DIR", root):
                self.assertEqual(
                    av1.ensure_under_data_dir(root / "jobs" / "job-1", name="job"),
                    root / "jobs" / "job-1",
                )
                with self.assertRaisesRegex(av1.HTTPException, "must be under"):
                    av1.ensure_under_data_dir(root / "input" / "clip.mp4", name="input")

    def test_startup_recovers_interrupted_jobs(self) -> None:
        calls: list[str] = []

        async def run_lifespan() -> None:
            async with av1.app.router.lifespan_context(av1.app):
                pass

        with patch.object(
            av1,
            "mark_interrupted_jobs_on_startup",
            side_effect=lambda: calls.append("recovered"),
        ):
            asyncio.run(run_lifespan())

        self.assertEqual(calls, ["recovered"])

    def test_profile_source_artifact_drops_require_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "reason"):
            av1.EncodeProfile.model_validate(
                {
                    "schema_version": 1,
                    "source": {
                        "artifact_drops": [
                            {
                                "selector": "stream:7",
                                "reason": " ",
                            }
                        ]
                    },
                }
            )

    def test_archive_item_uses_shared_strict_source_artifacts_builder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "clip.mkv"
            source.write_bytes(b"source")

            def fake_run_command(cmd: list[str], *, action: str, dry_run: bool = False) -> dict:
                output.write_bytes(b"before-source-artifacts")
                return {
                    "command": cmd,
                    "duration_s": 1.25,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                }

            def fake_build_source_artifacts(
                *,
                source: Path,
                archive_mkv: Path,
                encode_command: list[str],
                encode_profile: dict,
                source_sidecars: list[dict] | None = None,
            ) -> dict:
                self.assertEqual(source.name, "clip.mp4")
                self.assertEqual(archive_mkv, output)
                self.assertEqual(encode_command, ["ffmpeg", "-i", "clip.mp4", "clip.mkv"])
                self.assertEqual(encode_profile["name"], "test-profile")
                self.assertIsNone(source_sidecars)
                archive_mkv.write_bytes(b"after-source-artifacts")
                return {"output": str(archive_mkv) + ".source-artifacts.tar.zst"}

            with (
                patch.object(av1, "run_command", fake_run_command),
                patch.object(av1, "build_strict_source_artifacts", fake_build_source_artifacts),
            ):
                result = av1.run_encode_item(
                    ["ffmpeg", "-i", "clip.mp4", "clip.mkv"],
                    output_path=output,
                    action="archive video encode",
                    dry_run=False,
                    source_artifacts_source=source,
                    source_artifacts_profile={"name": "test-profile"},
                )

            self.assertEqual(result["bytes"], len(b"after-source-artifacts"))
            self.assertEqual(
                result["sha256"],
                hashlib.sha256(b"after-source-artifacts").hexdigest(),
            )
            self.assertEqual(
                result["source_artifacts"],
                {"output": str(output) + ".source-artifacts.tar.zst"},
            )

    def test_archive_item_reports_source_vanished_during_encode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "clip.webm"
            source.write_bytes(b"source")

            def fake_run_command(cmd: list[str], *, action: str, dry_run: bool = False) -> dict:
                source.unlink()
                raise RuntimeError("ffmpeg failed with 1: No such file or directory")

            with patch.object(av1, "run_command", fake_run_command):
                with self.assertRaisesRegex(av1.InputVanishedDuringJob, "source disappeared"):
                    av1.run_encode_item(
                        ["ffmpeg", "-i", str(source), str(output)],
                        output_path=output,
                        action="archive video encode",
                        dry_run=False,
                        source_artifacts_source=source,
                        source_artifacts_profile={"name": "test-profile"},
                    )

    def test_encode_item_recreates_output_parent_immediately_before_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "clip.mp4"
            output = root / "archive" / "nested" / "clip.mkv"
            source.write_bytes(b"source")
            output.parent.mkdir(parents=True)

            def remove_output_parent() -> None:
                output.parent.rmdir()

            def fake_run_command(cmd: list[str], *, action: str, dry_run: bool = False) -> dict:
                self.assertTrue(output.parent.is_dir())
                output.write_bytes(b"encoded")
                return {
                    "command": cmd,
                    "duration_s": 1.0,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                }

            with patch.object(av1, "run_command", fake_run_command):
                result = av1.run_encode_item(
                    ["ffmpeg", "-i", str(source), str(output)],
                    output_path=output,
                    action="archive video encode",
                    dry_run=False,
                    on_start=remove_output_parent,
                )

            self.assertEqual(result["bytes"], len(b"encoded"))

    def test_archive_batch_uses_server_projected_metadata_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            source = input_root / "clip.mp4"
            source.write_bytes(b"source")

            results = av1.run_batch(
                sources=[source],
                input_root=input_root,
                output_root=output_root,
                suffix=".webm",
                command_builder=lambda src, dest, metadata: [
                    "ffmpeg",
                    "-i",
                    str(src),
                    str(dest),
                ],
                label="archive video encode",
                dry_run=True,
                source_artifacts=True,
                source_artifacts_profile={"name": "test-profile"},
                container_metadata_required=False,
            )

            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["dry_run"])

    def test_archive_batch_requires_projected_container_metadata_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            source = input_root / "clip.mp4"
            source.write_bytes(b"source")

            with self.assertRaisesRegex(RuntimeError, "container metadata is required"):
                av1.run_batch(
                    sources=[source],
                    input_root=input_root,
                    output_root=output_root,
                    suffix=".webm",
                    command_builder=lambda src, dest, metadata: [
                        "ffmpeg",
                        "-i",
                        str(src),
                        str(dest),
                    ],
                    label="archive video encode",
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
