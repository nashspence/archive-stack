from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"
SPEC = importlib.util.spec_from_file_location("munchy_av1_main", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load {MODULE_PATH}")
av1 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = av1
SPEC.loader.exec_module(av1)


class SourceArtifactsTests(unittest.TestCase):
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
                source_filesystem_metadata: dict,
            ) -> dict:
                self.assertEqual(source.name, "clip.mp4")
                self.assertEqual(archive_mkv, output)
                self.assertEqual(encode_command, ["ffmpeg", "-i", "clip.mp4", "clip.mkv"])
                self.assertEqual(encode_profile["name"], "test-profile")
                self.assertEqual(source_filesystem_metadata["stat"]["st_birthtime"], 1.25)
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
                    source_filesystem_metadata={"stat": {"st_birthtime": 1.25}},
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

    def test_archive_batch_requires_source_filesystem_metadata_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            source = input_root / "clip.mp4"
            source.write_bytes(b"source")

            with self.assertRaisesRegex(RuntimeError, "unresumable.*filesystem metadata"):
                av1.run_batch(
                    sources=[source],
                    input_root=input_root,
                    output_root=output_root,
                    suffix=".webm",
                    command_builder=lambda src, dest: ["ffmpeg", "-i", str(src), str(dest)],
                    label="archive video encode",
                    dry_run=True,
                    source_artifacts=True,
                    source_artifacts_profile={"name": "test-profile"},
                )


if __name__ == "__main__":
    unittest.main()
