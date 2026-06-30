from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from munchy.metadata_projection import (
    MetadataProjectionError,
    ffmpeg_container_metadata_args,
    immich_xmp_sidecar_path,
    merge_immich_xmp_sidecar,
    project_immich_metadata,
    render_immich_xmp_sidecar,
)

DEFAULT_PROJECTION_CONFIG = {
    "device_make": "Apple",
    "device_model": "iPhone SE (2nd generation)",
    "creators": ["Nash Spence"],
}


def project_test_metadata(facts, **kwargs):  # type: ignore[no-untyped-def]
    config = {**DEFAULT_PROJECTION_CONFIG, **kwargs}
    return project_immich_metadata(facts, **config)


def test_immich_projection_maps_exif_date_and_dms_gps() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40-0700",
            "exif.gps_latitude": "37 deg 19' 54.36\" N",
            "exif.gps_longitude": "122 deg 1' 49.08\" W",
        },
        tags=["iphone-se2", "iphone-se2"],
    )

    assert metadata.capture_date == "2026-06-28T20:30:40-07:00"
    assert metadata.capture_date_source == "exif.date_time_original"
    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(37.3317666, abs=0.000001)
    assert metadata.gps.longitude == pytest.approx(-122.0303, abs=0.000001)
    assert metadata.device_make == "Apple"
    assert metadata.device_model == "iPhone SE (2nd generation)"
    assert metadata.creators == ("Nash Spence",)
    assert metadata.tags == ("iphone-se2",)

    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")

    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in xmp
    assert 'xmp:CreateDate="2026-06-28T20:30:40-07:00"' in xmp
    assert "xmp:CreationDate" not in xmp
    assert "xmp:ModifyDate" not in xmp
    assert 'photoshop:DateCreated="2026-06-28T20:30:40-07:00"' in xmp
    assert 'xmpDM:shotDate="2026-06-28T20:30:40-07:00"' in xmp
    assert 'tiff:Make="Apple"' in xmp
    assert 'tiff:Model="iPhone SE (2nd generation)"' in xmp
    assert 'exif:GPSLatitude="37,19.906000N"' in xmp
    assert 'exif:GPSLongitude="122,1.818000W"' in xmp
    assert 'geo:lat="37.33176667"' in xmp
    assert 'geo:long="-122.0303"' in xmp
    assert "<dc:subject>" in xmp
    assert "<dc:creator>" in xmp
    assert "<digiKam:TagsList>" in xmp
    assert "<lr:hierarchicalSubject>" in xmp
    assert "<Iptc4xmpCore:Keywords>" not in xmp
    assert "<rdf:li>Nash Spence</rdf:li>" in xmp
    assert "<rdf:li>iphone-se2</rdf:li>" in xmp
    ET.fromstring(xmp)


def test_immich_projection_maps_nested_ffprobe_apple_location() -> None:
    metadata = project_test_metadata(
        {
            "ffprobe": {
                "format_tags": {
                    "creation_time": "2026-06-28T21:30:40.123Z",
                    "com.apple.quicktime.location.iso6709": "+37.3317-122.0301+15.5/",
                }
            }
        }
    )

    assert metadata.capture_date == "2026-06-28T21:30:40.123Z"
    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(37.3317)
    assert metadata.gps.longitude == pytest.approx(-122.0301)
    assert metadata.gps.altitude == pytest.approx(15.5)
    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")
    assert 'exif:GPSAltitude="31/2"' in xmp
    assert 'exif:GPSAltitudeRef="0"' in xmp


def test_immich_projection_honors_full_word_gps_refs() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40",
            "exif.gps_latitude": 48.99951389,
            "exif.gps_latitude_ref": "North",
            "exif.gps_longitude": 122.74040278,
            "exif.gps_longitude_ref": "West",
        }
    )

    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(48.99951389)
    assert metadata.gps.longitude == pytest.approx(-122.74040278)
    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")
    assert 'exif:GPSLongitude="122,44.424167W"' in xmp
    assert 'geo:long="-122.74040278"' in xmp


def test_immich_projection_honors_embedded_decimal_hemisphere() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40",
            "exif.gps_latitude": "48.99950000 N",
            "exif.gps_longitude": "122.74060000 W",
        }
    )

    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(48.9995)
    assert metadata.gps.longitude == pytest.approx(-122.7406)


def test_immich_projection_writes_negative_altitude_ref() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40",
            "exif.gps_latitude": "37.1",
            "exif.gps_longitude": "-122.1",
            "exif.gps_altitude": "19.587",
            "exif.gps_altitude_ref": "below sea level",
        }
    )

    assert metadata.gps is not None
    assert metadata.gps.altitude == pytest.approx(-19.587)
    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")
    assert 'exif:GPSAltitude="19587/1000"' in xmp
    assert 'exif:GPSAltitudeRef="1"' in xmp


def test_immich_projection_uses_configured_path_regex_capture_date() -> None:
    metadata = project_test_metadata(
        {
            "path.rel": "VOICE/REC_20260628_203040.WAV",
            "exif.gps_latitude": "37.1",
            "exif.gps_longitude": "-122.1",
        },
        capture_date_sources=[
            {"type": "embedded"},
            {
                "type": "path_regex",
                "name": "voice_filename",
                "pattern": r"REC_(?P<stamp>\d{8}_\d{6})\.WAV$",
                "datetime_group": "stamp",
                "format": "%Y%m%d_%H%M%S",
                "timezone": "America/Los_Angeles",
            },
        ],
    )

    assert metadata.capture_date == "2026-06-28T20:30:40-07:00"
    assert metadata.capture_date_source == "path_regex:voice_filename"


def test_immich_projection_uses_filesystem_birthtime_capture_date() -> None:
    metadata = project_test_metadata(
        {
            "filesystem": {
                "stat": {
                    "birthtime": "2026-06-28T20:30:40+00:00",
                }
            },
            "exif.gps_latitude": "37.1",
            "exif.gps_longitude": "-122.1",
        },
        capture_date_sources=[
            {"type": "embedded"},
            {"type": "filesystem_birthtime", "name": "source_birthtime"},
        ],
    )

    assert metadata.capture_date == "2026-06-28T20:30:40+00:00"
    assert metadata.capture_date_source == "filesystem_birthtime:source_birthtime"


def test_immich_projection_uses_filesystem_birthtime_ns_capture_date() -> None:
    metadata = project_test_metadata(
        {
            "filesystem": {"stat": {"birthtime_ns": 1782682240000000000}},
            "exif.gps_latitude": "37.1",
            "exif.gps_longitude": "-122.1",
        },
        capture_date_sources=[{"type": "filesystem_birthtime"}],
    )

    assert metadata.capture_date == "2026-06-28T21:30:40+00:00"
    assert metadata.capture_date_source == "filesystem_birthtime:source_birthtime"


def test_immich_projection_can_use_xmp_sidecar_evidence() -> None:
    metadata = project_test_metadata(
        {
            "sidecars.ids": ["xmp"],
            "sidecars.xmp.facts.exif.date_time_original": "2026:06:28 20:30:40-0700",
            "sidecars.xmp.facts.exif.gps_latitude": "48.99950000 N",
            "sidecars.xmp.facts.exif.gps_longitude": "122.74060000 W",
        },
        capture_date_sources=[
            {"type": "embedded"},
            {"type": "sidecar", "id": "xmp"},
        ],
        gps_sources=[
            {"type": "embedded"},
            {"type": "sidecar", "id": "xmp"},
        ],
    )

    assert metadata.capture_date == "2026-06-28T20:30:40-07:00"
    assert metadata.capture_date_source == "sidecar:xmp:exif.date_time_original"
    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(48.9995)
    assert metadata.gps.longitude == pytest.approx(-122.7406)
    assert metadata.gps_source == "sidecar:xmp:exif.gps_latitude+exif.gps_longitude"


def test_immich_projection_filesystem_birthtime_must_parse() -> None:
    with pytest.raises(MetadataProjectionError, match="invalid capture date"):
        project_test_metadata(
            {
                "filesystem": {"stat": {"birthtime": "not a date"}},
                "exif.gps_latitude": "37.1",
                "exif.gps_longitude": "-122.1",
            },
            capture_date_sources=[{"type": "filesystem_birthtime"}],
        )


def test_immich_projection_path_regex_match_must_parse() -> None:
    with pytest.raises(MetadataProjectionError, match="matched but did not parse"):
        project_test_metadata(
            {
                "path.rel": "VOICE/REC_20261340_203040.WAV",
                "exif.gps_latitude": "37.1",
                "exif.gps_longitude": "-122.1",
            },
            capture_date_sources=[
                {
                    "type": "path_regex",
                    "name": "voice_filename",
                    "pattern": r"REC_(?P<stamp>\d{8}_\d{6})\.WAV$",
                    "datetime_group": "stamp",
                    "format": "%Y%m%d_%H%M%S",
                    "timezone": "America/Los_Angeles",
                }
            ],
        )


def test_immich_projection_path_regex_requires_timezone_without_offset() -> None:
    with pytest.raises(MetadataProjectionError, match="requires timezone"):
        project_test_metadata(
            {
                "path.rel": "VOICE/REC_20260628_203040.WAV",
                "exif.gps_latitude": "37.1",
                "exif.gps_longitude": "-122.1",
            },
            capture_date_sources=[
                {
                    "type": "path_regex",
                    "name": "voice_filename",
                    "pattern": r"REC_(?P<stamp>\d{8}_\d{6})\.WAV$",
                    "datetime_group": "stamp",
                    "format": "%Y%m%d_%H%M%S",
                }
            ],
        )


def test_immich_projection_writes_hierarchical_tag_aliases() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40",
            "exif.gps_latitude": "37.1",
            "exif.gps_longitude": "-122.1",
        },
        tags=["device/nash-iphone-se2", "device/nash-iphone-se2", "munchy/route/video"],
    )

    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")

    assert xmp.count("<rdf:li>device/nash-iphone-se2</rdf:li>") == 2
    assert "<lr:hierarchicalSubject>" in xmp
    assert "<rdf:li>device|nash-iphone-se2</rdf:li>" in xmp
    assert "<rdf:li>munchy|route|video</rdf:li>" in xmp


def test_immich_xmp_sidecar_roundtrips_with_exiftool_when_available(tmp_path) -> None:  # type: ignore[no-untyped-def]
    exiftool = shutil.which("exiftool")
    if exiftool is None:
        pytest.skip("exiftool is not installed")
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40-0700",
            "exif.gps_latitude": "48.99950000 N",
            "exif.gps_longitude": "122.74060000 W",
        },
        tags=["device/nash-iphone-se2"],
    )
    sidecar = tmp_path / "IMG_0001.HEIC.xmp"
    sidecar.write_text(
        render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z"),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [exiftool, "-j", "-G1", "-s", "-XMP:All", str(sidecar)],
        text=True,
        check=True,
        capture_output=True,
    )
    payload = json.loads(proc.stdout)[0]

    assert payload["XMP-xmp:CreateDate"] == "2026:06:28 20:30:40-07:00"
    assert "XMP-xmp:CreationDate" not in payload
    assert "XMP-xmp:ModifyDate" not in payload
    assert payload["XMP-exif:DateTimeOriginal"] == "2026:06:28 20:30:40-07:00"
    assert payload["XMP-photoshop:DateCreated"] == "2026:06:28 20:30:40-07:00"
    assert payload["XMP-xmpDM:ShotDate"] == "2026:06:28 20:30:40-07:00"
    assert payload["XMP-tiff:Make"] == "Apple"
    assert payload["XMP-tiff:Model"] == "iPhone SE (2nd generation)"
    assert payload["XMP-dc:Creator"] == "Nash Spence"
    assert payload["XMP-exif:GPSLongitude"] == "122 deg 44' 26.16\" W"
    assert payload["XMP-geo:Long"] == -122.7406
    assert payload["XMP-digiKam:TagsList"] == "device/nash-iphone-se2"
    assert payload["XMP-lr:HierarchicalSubject"] == "device|nash-iphone-se2"
    assert "XMP-iptcCore:Keywords" not in payload


def test_immich_projection_requires_date_and_gps_without_overrides() -> None:
    with pytest.raises(MetadataProjectionError, match="capture date"):
        project_test_metadata(
            {
                "exif.gps_latitude": "37.1",
                "exif.gps_longitude": "-122.1",
            }
        )

    with pytest.raises(MetadataProjectionError, match="GPS"):
        project_test_metadata({"exif.date_time_original": "2026:06:28 20:30:40"})

    metadata = project_test_metadata(
        {"exif.date_time_original": "2026:06:28 20:30:40"},
        allow_missing_gps=True,
    )
    assert metadata.capture_date == "2026-06-28T20:30:40"
    assert metadata.gps is None


def test_immich_projection_uses_configured_gps() -> None:
    metadata = project_test_metadata(
        {"exif.date_time_original": "2026:06:28 20:30:40"},
        configured_gps={
            "latitude": 48.999527523960296,
            "longitude": -122.74040765142755,
        },
    )

    assert metadata.gps is not None
    assert metadata.gps.latitude == pytest.approx(48.999527523960296)
    assert metadata.gps.longitude == pytest.approx(-122.74040765142755)
    assert metadata.gps_source == "metadata_projection.gps"


def test_immich_projection_rejects_invalid_configured_gps() -> None:
    with pytest.raises(MetadataProjectionError, match="metadata_projection.gps"):
        project_test_metadata(
            {"exif.date_time_original": "2026:06:28 20:30:40"},
            configured_gps={"latitude": 91.0, "longitude": -122.7404},
        )


def test_immich_projection_requires_configured_make_model_and_creators() -> None:
    facts = {
        "exif.date_time_original": "2026:06:28 20:30:40",
        "exif.gps_latitude": "37.1",
        "exif.gps_longitude": "-122.1",
    }

    with pytest.raises(MetadataProjectionError, match="device make"):
        project_immich_metadata(
            facts,
            device_model="iPhone SE (2nd generation)",
            creators=["Nash Spence"],
        )
    with pytest.raises(MetadataProjectionError, match="device model"):
        project_immich_metadata(
            facts,
            device_make="Apple",
            creators=["Nash Spence"],
        )
    with pytest.raises(MetadataProjectionError, match="creator"):
        project_immich_metadata(
            facts,
            device_make="Apple",
            device_model="iPhone SE (2nd generation)",
        )

    metadata = project_immich_metadata(
        facts,
        allow_missing_device_make=True,
        allow_missing_device_model=True,
        allow_missing_creators=True,
    )
    assert metadata.device_make is None
    assert metadata.device_model is None
    assert metadata.creators == ()


def test_immich_projection_writes_multiple_creators_and_container_metadata() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40-0700",
            "exif.gps_latitude": "48.99950000 N",
            "exif.gps_longitude": "122.74060000 W",
        },
        device_make="Sony",
        device_model="ILCE-6700",
        creators=["Alice Example", "Bob Example", "Alice Example"],
    )
    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")

    assert "<dc:creator>" in xmp
    assert "<rdf:li>Alice Example</rdf:li>" in xmp
    assert "<rdf:li>Bob Example</rdf:li>" in xmp
    assert xmp.index("<rdf:li>Alice Example</rdf:li>") < xmp.index(
        "<rdf:li>Bob Example</rdf:li>"
    )
    assert ffmpeg_container_metadata_args(metadata) == [
        "-metadata",
        "DATE=2026-06-28T20:30:40-07:00",
        "-metadata",
        "creation_time=2026-06-28T20:30:40-07:00",
        "-metadata",
        "ARTIST=Alice Example; Bob Example",
        "-metadata",
        "CREATOR=Alice Example; Bob Example",
        "-metadata",
        "MAKE=Sony",
        "-metadata",
        "MODEL=ILCE-6700",
        "-metadata",
        "LOCATION=+48.9995-122.7406/",
        "-metadata",
        "GPSLatitude=48.9995",
        "-metadata",
        "GPSLongitude=-122.7406",
    ]


def test_immich_xmp_sidecar_path_appends_xmp_to_output_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert immich_xmp_sidecar_path(tmp_path / "clip.webm").name == "clip.webm.xmp"


def test_immich_xmp_merge_preserves_existing_fields_and_adds_projection() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40-0700",
            "exif.gps_latitude": "48.99950000 N",
            "exif.gps_longitude": "122.74060000 W",
        },
        tags=["device/nash-iphone-se2"],
    )
    existing = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/"
                   xmlns:dc="http://purl.org/dc/elements/1.1/"
                   xmp:Label="keep-me">
   <dc:subject>
    <rdf:Bag>
     <rdf:li>existing-tag</rdf:li>
    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

    merged = merge_immich_xmp_sidecar(
        existing,
        metadata,
        metadata_date="2026-06-29T00:00:00Z",
    )

    assert 'xmp:Label="keep-me"' in merged
    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in merged
    assert 'tiff:Make="Apple"' in merged
    assert "<rdf:li>existing-tag</rdf:li>" in merged
    assert "<rdf:li>device/nash-iphone-se2</rdf:li>" in merged
    ET.fromstring(merged)


def test_immich_xmp_merge_rejects_scalar_conflict() -> None:
    metadata = project_test_metadata(
        {
            "exif.date_time_original": "2026:06:28 20:30:40-0700",
            "exif.gps_latitude": "48.99950000 N",
            "exif.gps_longitude": "122.74060000 W",
        }
    )
    existing = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Make="Other"/>
 </rdf:RDF>
</x:xmpmeta>
"""

    with pytest.raises(MetadataProjectionError, match="tiff:Make"):
        merge_immich_xmp_sidecar(
            existing,
            metadata,
            metadata_date="2026-06-29T00:00:00Z",
        )
