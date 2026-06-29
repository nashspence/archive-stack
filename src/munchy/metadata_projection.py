from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from html import escape
from pathlib import Path
from typing import Any


class MetadataProjectionError(ValueError):
    """Raised when source metadata cannot support the requested projection."""


@dataclass(frozen=True)
class GpsPosition:
    latitude: float
    longitude: float
    altitude: float | None = None

    def as_dict(self) -> dict[str, float]:
        out = {
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
        if self.altitude is not None:
            out["altitude"] = self.altitude
        return out

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GpsPosition | None:
        latitude = parse_float(value.get("latitude"))
        longitude = parse_float(value.get("longitude"))
        if latitude is None or longitude is None:
            return None
        position = cls(
            latitude=latitude,
            longitude=longitude,
            altitude=parse_float(value.get("altitude")),
        )
        return position if position.is_valid() else None

    def is_valid(self) -> bool:
        return -90.0 <= self.latitude <= 90.0 and -180.0 <= self.longitude <= 180.0


@dataclass(frozen=True)
class ProjectionMetadata:
    capture_date: str | None
    capture_date_source: str | None
    gps: GpsPosition | None
    gps_source: str | None
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "capture_date": self.capture_date,
            "capture_date_source": self.capture_date_source,
            "gps_source": self.gps_source,
            "tags": list(self.tags),
        }
        out["gps"] = self.gps.as_dict() if self.gps is not None else None
        return out

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProjectionMetadata:
        gps_value = value.get("gps")
        gps = GpsPosition.from_dict(gps_value) if isinstance(gps_value, Mapping) else None
        tags_value = value.get("tags")
        tags = tuple(
            str(item).strip()
            for item in tags_value
            if str(item).strip()
        ) if isinstance(tags_value, Sequence) and not isinstance(tags_value, str) else ()
        capture_date = value.get("capture_date")
        capture_date_source = value.get("capture_date_source")
        gps_source = value.get("gps_source")
        return cls(
            capture_date=str(capture_date) if capture_date else None,
            capture_date_source=str(capture_date_source) if capture_date_source else None,
            gps=gps,
            gps_source=str(gps_source) if gps_source else None,
            tags=tags,
        )


CAPTURE_DATE_FACT_KEYS = (
    "exif.sub_sec_date_time_original",
    "exif.date_time_original",
    "exif.sub_sec_create_date",
    "exif.creation_date",
    "exif.create_date",
    "exif.media_create_date",
    "exif.track_create_date",
    "exif.modify_date",
    "exiftool.tags.sub_sec_date_time_original",
    "exiftool.tags.date_time_original",
    "exiftool.tags.sub_sec_create_date",
    "exiftool.tags.creation_date",
    "exiftool.tags.create_date",
    "exiftool.tags.media_create_date",
    "exiftool.tags.track_create_date",
    "exiftool.tags.modify_date",
    "ffprobe.format_tags.com.apple.quicktime.creationdate",
    "ffprobe.format_tags.creation_time",
    "ffprobe.video_tags.creation_time",
    "ffprobe.stream_tags.creation_time",
    "ffprobe.audio_tags.creation_time",
)

GPS_PAIR_FACT_KEYS = (
    (
        "exif.gps_latitude",
        "exif.gps_longitude",
        "exif.gps_latitude_ref",
        "exif.gps_longitude_ref",
    ),
    (
        "exiftool.tags.gps_latitude",
        "exiftool.tags.gps_longitude",
        "exiftool.tags.gps_latitude_ref",
        "exiftool.tags.gps_longitude_ref",
    ),
)

GPS_POSITION_FACT_KEYS = (
    "exif.gps_position",
    "exif.gps_coordinates",
    "exif.location",
    "exif.location_iso6709",
    "exiftool.tags.gps_position",
    "exiftool.tags.gps_coordinates",
    "exiftool.tags.location",
    "exiftool.tags.location_iso6709",
    "ffprobe.format_tags.com.apple.quicktime.location.iso6709",
    "ffprobe.format_tags.location",
)

GPS_ALTITUDE_FACT_KEYS = (
    "exif.gps_altitude",
    "exiftool.tags.gps_altitude",
)

GPS_ALTITUDE_REF_KEYS = (
    "exif.gps_altitude_ref",
    "exiftool.tags.gps_altitude_ref",
)

EXIF_DATE_RE = re.compile(
    r"^(?P<year>\d{4}):(?P<month>\d{2}):(?P<day>\d{2})"
    r"[ T](?P<time>\d{2}:\d{2}:\d{2})(?P<fraction>\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)
ISO_DATE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)
ISO6709_RE = re.compile(
    r"(?P<lat>[+-]\d+(?:\.\d+)?)(?P<lon>[+-]\d+(?:\.\d+)?)"
    r"(?P<alt>[+-]\d+(?:\.\d+)?)?/?"
)


def immich_xmp_sidecar_path(primary_output: Path) -> Path:
    return primary_output.with_name(f"{primary_output.name}.xmp")


def project_immich_metadata(
    facts: Mapping[str, Any],
    *,
    allow_missing_capture_date: bool = False,
    allow_missing_gps: bool = False,
    tags: Sequence[str] = (),
) -> ProjectionMetadata:
    capture_date, capture_date_source = first_capture_date(facts)
    if capture_date is None and not allow_missing_capture_date:
        raise MetadataProjectionError(
            "metadata projection requires a valid capture date; set "
            "metadata_projection.allow_missing_capture_date=true to permit this source"
        )

    gps, gps_source = first_gps_position(facts)
    if gps is None and not allow_missing_gps:
        raise MetadataProjectionError(
            "metadata projection requires valid GPS coordinates; set "
            "metadata_projection.allow_missing_gps=true to permit this source"
        )

    return ProjectionMetadata(
        capture_date=capture_date,
        capture_date_source=capture_date_source,
        gps=gps,
        gps_source=gps_source,
        tags=normalized_tags(tags),
    )


def render_immich_xmp_sidecar(
    metadata: ProjectionMetadata,
    *,
    metadata_date: str,
) -> str:
    attrs = {
        "xmp:MetadataDate": metadata_date,
    }
    if metadata.capture_date:
        attrs["xmp:CreateDate"] = metadata.capture_date
        attrs["xmp:ModifyDate"] = metadata.capture_date
        attrs["exif:DateTimeOriginal"] = metadata.capture_date
        attrs["photoshop:DateCreated"] = metadata.capture_date
    if metadata.gps is not None:
        attrs["exif:GPSLatitude"] = xmp_gps_coordinate(metadata.gps.latitude, axis="lat")
        attrs["exif:GPSLongitude"] = xmp_gps_coordinate(metadata.gps.longitude, axis="lon")
        attrs["geo:lat"] = decimal_text(metadata.gps.latitude)
        attrs["geo:long"] = decimal_text(metadata.gps.longitude)
        if metadata.gps.altitude is not None:
            attrs["exif:GPSAltitude"] = rational_text(abs(metadata.gps.altitude))
            attrs["exif:GPSAltitudeRef"] = "1" if metadata.gps.altitude < 0 else "0"

    attr_lines = "\n".join(
        f'   {name}="{escape(str(value), quote=True)}"' for name, value in sorted(attrs.items())
    )
    tags = normalized_tags(metadata.tags)
    tag_blocks = ""
    if tags:
        tag_blocks = "\n".join(
            (
                render_rdf_bag("dc:subject", tags),
                render_rdf_seq("digiKam:TagsList", tags),
                render_rdf_bag("lr:hierarchicalSubject", hierarchical_tags(tags)),
            )
        )
        tag_blocks = f"\n{tag_blocks}"
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '   xmlns:xmp="http://ns.adobe.com/xap/1.0/"\n'
        '   xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
        '   xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"\n'
        '   xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '   xmlns:digiKam="http://www.digikam.org/ns/1.0/"\n'
        '   xmlns:lr="http://ns.adobe.com/lightroom/1.0/"\n'
        '   xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"\n'
        f"{attr_lines}>{tag_blocks}\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        '<?xpacket end="w"?>\n'
    )


def normalized_tags(tags: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))


def hierarchical_tags(tags: Sequence[str]) -> tuple[str, ...]:
    return tuple(tag.replace("/", "|") for tag in normalized_tags(tags))


def render_rdf_bag(name: str, values: Sequence[str]) -> str:
    return render_rdf_list(name, values, container="Bag")


def render_rdf_seq(name: str, values: Sequence[str]) -> str:
    return render_rdf_list(name, values, container="Seq")


def render_rdf_list(name: str, values: Sequence[str], *, container: str) -> str:
    tag_lines = "\n".join(f"     <rdf:li>{escape(value)}</rdf:li>" for value in values)
    return (
        f"  <{name}>\n"
        f"   <rdf:{container}>\n"
        f"{tag_lines}\n"
        f"   </rdf:{container}>\n"
        f"  </{name}>"
    )


def first_capture_date(facts: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for key in CAPTURE_DATE_FACT_KEYS:
        for value in metadata_values(fact_value(facts, key)):
            normalized = normalize_capture_date(value)
            if normalized:
                return normalized, key
    return None, None


def first_gps_position(facts: Mapping[str, Any]) -> tuple[GpsPosition | None, str | None]:
    altitude = first_altitude(facts)
    for lat_key, lon_key, lat_ref_key, lon_ref_key in GPS_PAIR_FACT_KEYS:
        lat_ref = first_metadata_value(fact_value(facts, lat_ref_key))
        lon_ref = first_metadata_value(fact_value(facts, lon_ref_key))
        latitude = parse_coordinate(fact_value(facts, lat_key), ref=lat_ref, axis="lat")
        longitude = parse_coordinate(fact_value(facts, lon_key), ref=lon_ref, axis="lon")
        if latitude is None or longitude is None:
            continue
        gps = GpsPosition(latitude=latitude, longitude=longitude, altitude=altitude)
        if gps.is_valid():
            return gps, f"{lat_key}+{lon_key}"

    for key in GPS_POSITION_FACT_KEYS:
        for value in metadata_values(fact_value(facts, key)):
            position = parse_gps_position(value, altitude=altitude)
            if position is not None:
                return position, key
    return None, None


def first_altitude(facts: Mapping[str, Any]) -> float | None:
    altitude = first_parsed_float(facts, GPS_ALTITUDE_FACT_KEYS)
    if altitude is None:
        return None
    ref = first_present_fact(facts, GPS_ALTITUDE_REF_KEYS)
    ref_text = str(ref or "").strip().lower()
    if ref_text in {"1", "below sea level", "below"}:
        return -abs(altitude)
    return altitude


def first_parsed_float(facts: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        for value in metadata_values(fact_value(facts, key)):
            parsed = parse_float(value)
            if parsed is not None:
                return parsed
    return None


def first_present_fact(facts: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = first_metadata_value(fact_value(facts, key))
        if value not in (None, ""):
            return value
    return None


def fact_value(facts: Mapping[str, Any], key: str) -> Any:
    if key in facts:
        return facts[key]
    current: Any = facts
    parts = key.split(".")
    for index, part in enumerate(parts):
        if not isinstance(current, Mapping):
            return None
        if part in current:
            current = current[part]
            continue
        remainder = ".".join(parts[index:])
        if remainder in current:
            return current[remainder]
        return None
    return current


def metadata_values(value: Any) -> Sequence[Any]:
    if value in (None, ""):
        return ()
    if isinstance(value, Sequence) and not isinstance(value, str):
        return value
    return (value,)


def first_metadata_value(value: Any) -> Any:
    values = metadata_values(value)
    return values[0] if values else None


def normalize_capture_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"N/A", "UNKNOWN"}:
        return None
    if text.startswith("0000:00:00") or text.startswith("0000-00-00"):
        return None
    match = EXIF_DATE_RE.match(text)
    if match:
        normalized = (
            f"{match.group('year')}-{match.group('month')}-{match.group('day')}"
            f"T{match.group('time')}{match.group('fraction') or ''}"
            f"{normalize_timezone(match.group('tz') or '')}"
        )
        return normalized if valid_iso_datetime(normalized) else None
    match = ISO_DATE_RE.match(text)
    if match:
        normalized = (
            f"{match.group('date')}T{match.group('time')}{match.group('fraction') or ''}"
            f"{normalize_timezone(match.group('tz') or '')}"
        )
        return normalized if valid_iso_datetime(normalized) else None
    return None


def normalize_timezone(value: str) -> str:
    if not value:
        return ""
    if value == "Z":
        return "Z"
    if re.match(r"^[+-]\d{2}\d{2}$", value):
        return f"{value[:3]}:{value[3:]}"
    return value


def valid_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def parse_gps_position(value: Any, *, altitude: float | None = None) -> GpsPosition | None:
    text = str(value or "").strip()
    if not text:
        return None
    iso_match = ISO6709_RE.fullmatch(text)
    if iso_match:
        lat = parse_float(iso_match.group("lat"))
        lon = parse_float(iso_match.group("lon"))
        alt = parse_float(iso_match.group("alt")) if iso_match.group("alt") else altitude
        gps = GpsPosition(latitude=lat or 0.0, longitude=lon or 0.0, altitude=alt)
        return gps if lat is not None and lon is not None and gps.is_valid() else None
    if "," in text:
        left, right = text.split(",", 1)
        lat = parse_coordinate(left, axis="lat")
        lon = parse_coordinate(right, axis="lon")
        if lat is not None and lon is not None:
            gps = GpsPosition(latitude=lat, longitude=lon, altitude=altitude)
            return gps if gps.is_valid() else None
    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if len(numbers) >= 2:
        lat = parse_float(numbers[0])
        lon = parse_float(numbers[1])
        if lat is not None and lon is not None:
            gps = GpsPosition(latitude=lat, longitude=lon, altitude=altitude)
            return gps if gps.is_valid() else None
    return None


def parse_coordinate(value: Any, *, ref: Any = None, axis: str) -> float | None:
    item = first_metadata_value(value)
    if item is None:
        return None
    text = str(item).strip()
    if not text:
        return None
    ref_text = str(ref or "").strip().upper()
    value_upper = text.upper()
    sign = -1.0 if text.startswith("-") or ref_text in {"S", "SOUTH", "W", "WEST"} else 1.0
    if re.search(r"\b(S|SOUTH|W|WEST)\b", value_upper):
        sign = -1.0
    if re.search(r"\bDEG\b|[\"']", value_upper):
        parsed_numbers: list[float] = []
        for number in re.findall(r"\d+(?:\.\d+)?", text):
            parsed = parse_float(number)
            if parsed is not None:
                parsed_numbers.append(parsed)
        if not parsed_numbers:
            return None
        decimal = parsed_numbers[0]
        if len(parsed_numbers) >= 2:
            decimal += parsed_numbers[1] / 60.0
        if len(parsed_numbers) >= 3:
            decimal += parsed_numbers[2] / 3600.0
        return valid_coordinate(decimal * sign, axis=axis)
    decimal_value = parse_float(text)
    if decimal_value is None:
        return None
    if sign < 0 and decimal_value > 0:
        decimal_value = -decimal_value
    return valid_coordinate(decimal_value, axis=axis)


def valid_coordinate(value: float, *, axis: str) -> float | None:
    if axis == "lat" and -90.0 <= value <= 90.0:
        return value
    if axis == "lon" and -180.0 <= value <= 180.0:
        return value
    return None


def xmp_gps_coordinate(value: float, *, axis: str) -> str:
    absolute = abs(value)
    degrees = int(absolute)
    minutes = (absolute - degrees) * 60.0
    hemisphere = "N" if axis == "lat" and value >= 0 else "S"
    if axis == "lon":
        hemisphere = "E" if value >= 0 else "W"
    return f"{degrees},{minutes:.6f}{hemisphere}"


def parse_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def decimal_text(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def rational_text(value: float) -> str:
    rational = Fraction(value).limit_denominator(1_000_000)
    return f"{rational.numerator}/{rational.denominator}"
