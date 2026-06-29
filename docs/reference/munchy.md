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
gpu_tasks = ["archive_video"]

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
- `xmp:CreationDate`
- `xmp:ModifyDate`
- `exif:DateTimeOriginal`
- `photoshop:DateCreated`

GPS is written both as EXIF-style coordinates and WGS84 decimal coordinates.
West and south coordinates must remain negative in the decimal fields.

`metadata_projection.tags` is the operator-facing tag concept. Munchy projects
the same tags into multiple XMP tag/keyword dialects for compatibility:

- `dc:subject`
- `digiKam:TagsList`
- `lr:HierarchicalSubject`
- `Iptc4xmpCore:Keywords`

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
