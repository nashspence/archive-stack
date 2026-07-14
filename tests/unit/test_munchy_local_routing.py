from __future__ import annotations

from pathlib import Path

import munchy.local_routing as local_routing
from munchy.local_files import LocalFileCandidate
from munchy.local_routing import routing_plan_files
from munchy.routing import routing_plan


def test_routing_plan_files_collects_only_declared_sidecar_tags(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    primary = tmp_path / "C0001.MP4"
    sidecar = tmp_path / "C0001M01.XML"
    primary.write_bytes(b"video")
    sidecar.write_text("<metadata />", encoding="utf-8")
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fail_ffprobe(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for sidecar-facts-only routing: {path}")

    def fake_exiftool(path: Path, *, tags: tuple[str, ...]) -> dict[str, object]:
        calls.append((path, tags))
        return {
            "SourceFile": str(path),
            "Make": "Example Imaging",
            "Model": "Synthetic Camera",
        }

    monkeypatch.setattr(local_routing, "ffprobe_for_routing", fail_ffprobe)
    monkeypatch.setattr(local_routing, "exiftool_for_routing", fake_exiftool)
    routing = {
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "paths": ["{parent}/{stem}M01.XML"],
                "primary": {"path": {"suffix": ".mp4"}},
                "sidecar": {"path": {"suffix": ".xml"}},
                "facts": {"source": "exiftool", "tags": ["Make", "Model"]},
            }
        ],
        "routes": [
            {
                "id": "sidecar-camera-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {
                            "fact": "sidecars.camera_xml.facts.exif.make",
                            "equals": "example imaging",
                        },
                    ]
                },
            }
        ],
    }
    candidates = [
        LocalFileCandidate(
            source=primary,
            rel_path="camera/C0001.MP4",
            bytes=primary.stat().st_size,
            mtime_ns=1,
        ),
        LocalFileCandidate(
            source=sidecar,
            rel_path="camera/C0001M01.XML",
            bytes=sidecar.stat().st_size,
            mtime_ns=2,
        ),
    ]

    files = routing_plan_files(candidates, routing=routing)
    plan = routing_plan(routing, files, group_names={"video"})

    assert calls == [(sidecar, ("Make", "Model"))]
    assert plan.ok
    assert plan.matched_files == 2
    assert plan.matches[0]["route_id"] == "sidecar-camera-video"
    assert plan.matches[1]["sidecar_id"] == "camera_xml"


def test_routing_plan_files_collects_primary_facts_after_sidecar_facts(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    primary = tmp_path / "C0001.MP4"
    sidecar = tmp_path / "C0001M01.XML"
    primary.write_bytes(b"video")
    sidecar.write_text("<metadata />", encoding="utf-8")
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fail_ffprobe(path: Path) -> dict[str, object]:
        raise AssertionError(f"ffprobe should not run for exiftool-only routing: {path}")

    def fake_exiftool(path: Path, *, tags: tuple[str, ...]) -> dict[str, object]:
        calls.append((path, tags))
        if path == sidecar:
            return {"EXIF:Make": "Example Imaging"}
        if path == primary:
            assert "VideoAvgBitrate" in tags
            return {"QuickTime:VideoAvgBitrate": "200 Mbps"}
        raise AssertionError(f"unexpected exiftool path: {path}")

    monkeypatch.setattr(local_routing, "ffprobe_for_routing", fail_ffprobe)
    monkeypatch.setattr(local_routing, "exiftool_for_routing", fake_exiftool)
    routing = {
        "extra_exiftool_tags": ["VideoAvgBitrate"],
        "sidecars": [
            {
                "id": "camera_xml",
                "format": "xml",
                "path": "{parent}/{stem}M01.XML",
                "primary": {"path": {"suffix": ".mp4"}},
                "facts": {"source": "exiftool", "tags": ["Make"]},
            }
        ],
        "routes": [
            {
                "id": "sidecar-and-bitrate-video",
                "group": "video",
                "when": {
                    "all": [
                        {"path": {"suffix": ".mp4"}},
                        {
                            "fact": "sidecars.camera_xml.facts.exif.make",
                            "equals": "example imaging",
                        },
                        {
                            "fact": "exiftool.tags.video_avg_bitrate",
                            "equals": "200 Mbps",
                        },
                    ]
                },
            }
        ],
    }
    candidates = [
        LocalFileCandidate(
            source=primary,
            rel_path="camera/C0001.MP4",
            bytes=primary.stat().st_size,
            mtime_ns=1,
        ),
        LocalFileCandidate(
            source=sidecar,
            rel_path="camera/C0001M01.XML",
            bytes=sidecar.stat().st_size,
            mtime_ns=2,
        ),
    ]

    files = routing_plan_files(candidates, routing=routing)
    plan = routing_plan(routing, files, group_names={"video"})

    assert [path for path, _tags in calls] == [sidecar, primary]
    assert calls[0] == (sidecar, ("Make",))
    assert plan.ok
    assert plan.matched_files == 2
    assert plan.matches[0]["route_id"] == "sidecar-and-bitrate-video"
    assert plan.matches[1]["sidecar_id"] == "camera_xml"
