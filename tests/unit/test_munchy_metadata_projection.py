from __future__ import annotations

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
    assert 'exif:GPSLatitude="37,19.906000N"' in xmp
    assert 'exif:GPSLongitude="122,1.818000W"' in xmp
    assert 'geo:lat="37.33176667"' in xmp
    assert 'geo:long="-122.0303"' in xmp
    assert "<rdf:li>iphone-se2</rdf:li>" in xmp


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
