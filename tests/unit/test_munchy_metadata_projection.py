from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from munchy.metadata_projection import (
    MetadataProjectionError,
    immich_xmp_sidecar_path,
    project_immich_metadata,
    render_immich_xmp_sidecar,
)


def test_immich_projection_maps_exif_date_and_dms_gps() -> None:
    metadata = project_immich_metadata(
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
    assert metadata.tags == ("iphone-se2",)

    xmp = render_immich_xmp_sidecar(metadata, metadata_date="2026-06-29T00:00:00Z")

    assert 'exif:DateTimeOriginal="2026-06-28T20:30:40-07:00"' in xmp
    assert 'xmp:CreateDate="2026-06-28T20:30:40-07:00"' in xmp
    assert "xmp:CreationDate" not in xmp
    assert 'xmp:ModifyDate="2026-06-28T20:30:40-07:00"' in xmp
    assert 'photoshop:DateCreated="2026-06-28T20:30:40-07:00"' in xmp
    assert 'exif:GPSLatitude="37,19.906000N"' in xmp
    assert 'exif:GPSLongitude="122,1.818000W"' in xmp
    assert 'geo:lat="37.33176667"' in xmp
    assert 'geo:long="-122.0303"' in xmp
    assert "<dc:subject>" in xmp
    assert "<digiKam:TagsList>" in xmp
    assert "<lr:hierarchicalSubject>" in xmp
    assert "<Iptc4xmpCore:Keywords>" not in xmp
    assert "<rdf:li>iphone-se2</rdf:li>" in xmp
    ET.fromstring(xmp)


def test_immich_projection_maps_nested_ffprobe_apple_location() -> None:
    metadata = project_immich_metadata(
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
    metadata = project_immich_metadata(
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
    metadata = project_immich_metadata(
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
    metadata = project_immich_metadata(
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


def test_immich_projection_writes_hierarchical_tag_aliases() -> None:
    metadata = project_immich_metadata(
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
    metadata = project_immich_metadata(
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
    assert payload["XMP-xmp:ModifyDate"] == "2026:06:28 20:30:40-07:00"
    assert payload["XMP-exif:DateTimeOriginal"] == "2026:06:28 20:30:40-07:00"
    assert payload["XMP-photoshop:DateCreated"] == "2026:06:28 20:30:40-07:00"
    assert payload["XMP-exif:GPSLongitude"] == "122 deg 44' 26.16\" W"
    assert payload["XMP-geo:Long"] == -122.7406
    assert payload["XMP-digiKam:TagsList"] == "device/nash-iphone-se2"
    assert payload["XMP-lr:HierarchicalSubject"] == "device|nash-iphone-se2"
    assert "XMP-iptcCore:Keywords" not in payload


def test_immich_projection_requires_date_and_gps_without_overrides() -> None:
    with pytest.raises(MetadataProjectionError, match="capture date"):
        project_immich_metadata(
            {
                "exif.gps_latitude": "37.1",
                "exif.gps_longitude": "-122.1",
            }
        )

    with pytest.raises(MetadataProjectionError, match="GPS"):
        project_immich_metadata({"exif.date_time_original": "2026:06:28 20:30:40"})

    metadata = project_immich_metadata(
        {"exif.date_time_original": "2026:06:28 20:30:40"},
        allow_missing_gps=True,
    )
    assert metadata.capture_date == "2026-06-28T20:30:40"
    assert metadata.gps is None


def test_immich_xmp_sidecar_path_appends_xmp_to_output_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert immich_xmp_sidecar_path(tmp_path / "clip.webm").name == "clip.webm.xmp"
