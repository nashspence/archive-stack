# Munchy

`munchy` is Riverhog's generic ingest and encode layer. It accepts an input
upload, applies optional ordered profile routing, writes archive outputs, and can
project source metadata into XMP sidecars next to those outputs.

## Metadata Projection

Metadata projection is enabled by default for groups that produce primary
archive outputs. Disable it per group only when that output must not get a
sidecar:

```toml
[groups.video]
archive_mode = "av1_nvenc"
tasks = ["archive_video"]

[groups.video.metadata_projection]
enabled = true
tags = ["device/example-camera"]
```

The only supported target is `immich_xmp`. Munchy writes sidecars as
`output.ext.xmp` next to the output file.

By default, projection requires a valid capture date and valid GPS coordinates.
Use explicit overrides for sources that are expected to lack those fields:

```toml
[groups.video.metadata_projection]
allow_missing_capture_date = false
allow_missing_gps = true
```

The sidecar includes capture date aliases that common XMP readers consume:

- `xmp:CreateDate`
- `exif:DateTimeOriginal`
- `photoshop:DateCreated`

`xmp:MetadataDate` records when the sidecar metadata was generated or updated.
Munchy intentionally omits `xmp:ModifyDate` because archived outputs may be
transcoded derivatives rather than the original captured resource.

Capture date sources are ordered. By default Munchy uses embedded metadata.
Recorder-style workflows may opt into source filesystem birth time, or a parsed
path regex, as explicit fallbacks:

```toml
[groups.voice.metadata_projection]
allow_missing_gps = true
capture_date_sources = [
  { type = "embedded" },
  { type = "filesystem_birthtime" },
  { type = "path_regex", name = "voice_filename", pattern = "REC_(?P<stamp>[0-9]{8}_[0-9]{6})\\.WAV$", datetime_group = "stamp", format = "%Y%m%d_%H%M%S", timezone = "America/Los_Angeles" },
]
```

If a configured regex or filesystem birth time source is present but invalid,
projection fails rather than guessing.

GPS is written both as EXIF-style coordinates and WGS84 decimal coordinates.
West and south coordinates must remain negative in the decimal fields. Altitude
is written as an EXIF XMP rational with `exif:GPSAltitudeRef`.

`metadata_projection.tags` is the operator-facing tag concept. Munchy projects
the same tags into multiple XMP tag/keyword dialects for compatibility:

- `dc:subject`
- `digiKam:TagsList`
- `lr:hierarchicalSubject`

When `include_context_tags` is true, which is the default, Munchy also adds tags
for context that is already known at sidecar time:

- `munchy/collection/<collection-slug>`
- `munchy/group/<group-name>`
- `munchy/route/<route-id>`
- `munchy/output/<output-directory>`
- `munchy/pair/<pair-kind>`
- `munchy/pair/<pair-kind>/<pair-role>`

Use `include_context_tags = false` if the sidecar should contain only the
configured tags. Public Munchy does not invent private device identity; callers
that know device identity should pass it as a configured tag such as
`device/example-camera`.

## Audio Archive

Audio-only archive groups use `archive_mode = "audio"` and the `archive_audio`
task. If `tasks` is omitted on an audio group, Munchy defaults it to
`["archive_audio"]`. Audio archive work runs on CPU and does not reserve the GPU
target.

```toml
[profiles.voice]
schema_version = 1
target = "munchy-audio"
name = "voice"

[profiles.voice.archive]
codec = "opus"

[profiles.voice.archive.audio]
bitrate = "64k"
sample_rate = 24000
channels = 1
application = "audio"

[groups.voice]
profile = "voice"
archive_mode = "audio"
```

Munchy currently supports Opus audio archives in an `.opus` container. Source
filesystem metadata sidecars are required for audio archive jobs so the archive
can preserve custody metadata with the encoded output.
