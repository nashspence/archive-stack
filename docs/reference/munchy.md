# Munchy

`munchy` is Riverhog's generic ingest and encode layer. It accepts an input
upload, applies optional ordered profile routing, writes archive outputs, and can
project source metadata into XMP sidecars next to those outputs.

## Profile Routing

Profile routing is ordered. Each route is evaluated against files that have not
already matched an earlier route. A file that falls through all routes is a
preflight failure unless it was intentionally matched by a `leave` route.

Routes can match against path facts, ffprobe facts, and ExifTool facts. Profiles
may also declare sidecar evidence rules. A sidecar evidence file is not archived
as its own primary output; it is attached to its primary file for metadata
projection and source-artifacts custody.

```yaml
job:
  routing:
    sidecars:
      xmp:
        format: xmp
        primary:
          path:
            suffix_in:
              - .heic
              - .jpg
              - .mov
    routes:
      - id: camera-video
        group: video
        when:
          path:
            suffix: .mov
```

For `format = "xmp"`, Munchy looks for `primary-path.xmp` and
`primary-stem.xmp` in the same directory unless explicit `path` or `paths`
templates are configured.

Sidecar rules can opt into parsed routing facts from matched evidence sidecars.
This is generic and is not tied to a specific device format. The sidecar rule
must name the bounded ExifTool tag set to read; Munchy then exposes the parsed
facts on the primary routing context under
`sidecars.<sidecar-id>.facts.*`:

```yaml
job:
  routing:
    sidecars:
      camera_xml:
        format: xml
        path: "{parent}/{stem}M01.XML"
        primary:
          path:
            suffix: .mp4
        facts:
          source: exiftool
          tags:
            - Make
            - Model
            - CaptureFPS
    routes:
      - id: xml-described-camera-video
        group: video
        when:
          all:
            - path:
                suffix: .mp4
            - fact: sidecars.camera_xml.facts.exif.make
              equals: example imaging
            - fact: sidecars.camera_xml.facts.exiftool.tags.capture_fps
              min: 100
```

When a `facts` block is configured for a matched primary, the sidecar and its
parsed facts become a preflight requirement. A missing sidecar, missing parsed
facts, or failed sidecar parse returns a `sidecar_facts_failed` routing failure
instead of falling through to a broader route.

Sidecar evidence inherits the primary file's routing disposition. If the primary
is archived, the sidecar is recorded in source artifacts. If the primary is
intentionally left, the sidecar is left too. If the primary is unmatched, the
sidecar remains a failed preflight condition.

Evidence sidecars are never primary collection outputs. The routing manifest
records them with `route.action = "evidence"` and
`output = { kind = "none", reason = "sidecar_evidence" }`. When the primary is
archived, the evidence entry also records source-artifacts custody:

```json
{
  "route": {
    "action": "evidence",
    "sidecar": {
      "id": "xmp",
      "format": "xmp",
      "for": "camera/IMG_0001.MOV"
    }
  },
  "output": {
    "kind": "none",
    "reason": "sidecar_evidence"
  },
  "custody": {
    "kind": "source_artifact_sidecar",
    "primary_source": "camera/IMG_0001.MOV",
    "source_artifacts_entry": "sidecars/camera/IMG_0001.MOV.xmp"
  }
}
```

`archive_mode: preserve` copies the primary source bytes to the collection
archive output without mutating them. If an existing XMP sidecar is attached as
evidence for a preserve output, Munchy writes the visible output XMP by merging
the normalized projected metadata into that existing XMP. Scalar conflicts fail
the job instead of overwriting operator or source-provided metadata.

## Metadata Projection

Metadata projection is enabled by default for groups that produce primary
archive outputs. Disable it per group only when that output must not get a
sidecar:

```yaml
groups:
  video:
    archive_mode: av1_nvenc
    tasks:
      - archive_video
    metadata_projection:
      enabled: true
      creators:
        - Example Operator
      tags:
        - device/example-camera
      device:
        make: Example
        model: Camera
      gps:
        latitude: 48.999527523960296
        longitude: -122.74040765142755
```

The only supported target is `immich_xmp`. Munchy writes sidecars as
`output.ext.xmp` next to the output file.

Profile-routing preflight returns a `readout` block in addition to the routing
plan. The readout is intended for operator tooling: it lists sidecars attached
to each primary and summarizes metadata projection resolution, including the
selected capture date source and GPS source when the submitted facts are enough
to resolve them. Routing `ok` remains the routing result; readout metadata
errors are diagnostic unless a caller submits
`enforce_metadata_projection: true`. In enforced mode, a routed upload whose
metadata projection cannot satisfy the configured requirements is returned as an
unmatched file with `reason: metadata_projection_failed` while the readout still
shows the full projection diagnostic.

By default, projection requires a valid capture date, valid GPS coordinates,
configured device make, configured device model, and at least one configured
creator. Use explicit overrides for sources that are expected to lack those
fields:

```yaml
groups:
  video:
    metadata_projection:
      allow_missing_capture_date: false
      allow_missing_gps: true
      allow_missing_device_make: false
      allow_missing_device_model: false
      allow_missing_creators: false
```

The sidecar includes capture date aliases that common XMP readers consume:

- `xmp:CreateDate`
- `exif:DateTimeOriginal`
- `photoshop:DateCreated`
- `xmpDM:shotDate`

Configured device identity is projected into `tiff:Make` and `tiff:Model`.
Configured creators are projected as an ordered `dc:creator` RDF sequence.

`xmp:MetadataDate` records when the sidecar metadata was generated or updated.
Munchy intentionally omits `xmp:ModifyDate` because archived outputs may be
transcoded derivatives rather than the original captured resource.

Capture date sources are ordered. By default Munchy uses embedded metadata.
Recorder-style workflows may opt into source filesystem birth time, or a parsed
path regex, as explicit fallbacks:

```yaml
groups:
  voice:
    metadata_projection:
      allow_missing_gps: true
      creators:
        - Example Operator
      capture_date_sources:
        - type: embedded
        - type: filesystem_birthtime
        - type: path_regex
          name: voice_filename
          pattern: 'REC_(?P<stamp>[0-9]{8}_[0-9]{6})\.WAV$'
          datetime_group: stamp
          format: "%Y%m%d_%H%M%S"
          timezone: America/Los_Angeles
      device:
        make: Example
        model: Voice Recorder
```

If a configured regex or filesystem birth time source is present but invalid,
projection fails rather than guessing.

Sidecar capture-date sources can use the usual embedded capture-date keys from
parsed sidecar facts, or can name one or more explicit fact keys when a vendor
sidecar carries the authoritative timestamp under a nonstandard tag:

```yaml
groups:
  video:
    metadata_projection:
      capture_date_sources:
        - type: sidecar
          id: camera_xml
          fact: exiftool.tags.vendor_capture_date
        - type: embedded
```

The `fact` and `facts` keys are relative to the parsed sidecar fact namespace.
For example, `exiftool.tags.vendor_capture_date` reads
`sidecars.camera_xml.facts.exiftool.tags.vendor_capture_date` from the primary
file's routing context. A configured sidecar date fact that is present but does
not parse as an EXIF-style or ISO-style timestamp fails projection instead of
silently falling through.

GPS is written both as EXIF-style coordinates and WGS84 decimal coordinates.
West and south coordinates must remain negative in the decimal fields. Altitude
is written as an EXIF XMP rational with `exif:GPSAltitudeRef`.
For fixed-location devices, `metadata_projection.gps` can provide the
authoritative GPS position when files do not embed coordinates.

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
that know device identity should pass configured device fields, creators, and a
device tag such as `device/example-camera`.

For encoded audio and video outputs, Munchy also writes the same projected
capture date, GPS, device, and creator metadata into ffmpeg container metadata
where the output container supports scalar tags. Preserve archive outputs are
not mutated; they receive XMP sidecars only.

## New Device Configs

Create new device configs from original source exports and keep routing proof
separate from encode-quality proof. The canonical demo layout is:

```text
<device>/
  routing-demos/
  encode-tuning/
```

A routing demo set should contain tiny, representative originals that exercise
every expected capture profile and edge case. Include sidecars, paired captures,
sent or downloaded media, screenshots, high-frame-rate modes, audio-only modes,
or any other source shape the device can produce. Use this set to iterate on
ordered routes, output group names, sidecar attachment, metadata projection
sources, and fallthrough behavior. The routing set should stay small enough that
profile-routing preflight and collection-archive target runs are cheap and
repeatable.

An encode tuning set should contain fuller realistic examples for routes that
transcode. Include hard cases such as motion, low light, high detail, long
duration, noisy audio, or other examples that reveal quality and size tradeoffs.
Use this set only after routing is correct, then tune profile settings by
inspecting the generated collection outputs and source artifacts.

The preferred enrollment loop is:

1. Collect a small routing demo set from original exports.
2. Write the profile groups, routes, sidecar rules, and metadata projection
   settings.
3. Run preflight and inspect the readout for route ids, attached sidecars,
   capture date source, GPS source, device metadata, and unmatched files.
4. Run a collection archive to a target destination, not Riverhog, and inspect
   the collection tree, XMP sidecars, source artifacts, and routing manifest.
5. Collect fuller encode tuning examples for routes that transcode.
6. Tune encode settings against those examples before using the config for
   Riverhog archival handoff.

## Audio Archive

Audio-only archive groups use `archive_mode: audio` and the `archive_audio`
task. If `tasks` is omitted on an audio group, Munchy defaults it to
`["archive_audio"]`. Audio archive work runs on CPU and does not reserve the GPU
target.

```yaml
profiles:
  voice:
    schema_version: 1
    target: munchy-audio
    name: voice
    archive:
      codec: opus
      audio:
        bitrate: 64k
        sample_rate: 24000
        channels: 1
        application: audio
    source:
      allow_conversion_only_container: true

groups:
  voice:
    profile: voice
    archive_mode: audio
```

Munchy currently supports Opus audio archives in an `.opus` container. Source
filesystem metadata sidecars are required for audio archive jobs so the archive
can preserve custody metadata with the encoded output.

When metadata projection is enabled, Munchy resolves the same capture date and
GPS metadata used for XMP before encoding the `.opus` file. The Opus output gets
Vorbis-comment style metadata for capture date, GPS, configured device make and
model, and configured creators. XMP sidecars remain the richer compatibility
target.

Sources such as MP3 cannot be reconstructed from an Opus derivative because the
encoded audio stream cannot be muxed back into the original MP3 container.
Profiles for those sources must explicitly set
`source.allow_conversion_only_container: true`; otherwise Munchy refuses the
job rather than silently accepting conversion-only custody semantics. Leave the
option disabled for camera/video sources that require source-container rebuild
support from the archived output and source artifacts.

Source artifact bundles preserve strict source inventory, filesystem metadata,
stream transforms, and source metadata. Rebuild support is represented in
`inventory/source-inventory.json`; operator-facing rebuild guidance can be
derived from source inventory plus `encoding/stream-transforms.json`.

## Review Uploads

Collection-archive target and review uploads can be handed off through rclone. Munchy
filters common desktop platform helper files by default before counting or
uploading review artifacts:

- `.DS_Store`
- `._*`
- `.Spotlight-V100/`
- `.Trashes/`
- `.fseventsd/`

Operators may add project-specific rclone exclude patterns:

```yaml
job:
  review_upload:
    enabled: true
    method: rclone
    destination: "clover:munchy/{collection_slug}/{collection_timestamp}"
    exclude:
      - "**/.temporary/**"
```

If a runner job fails after an operator fixes the cause, resume it explicitly:

```bash
munchy job resume <job-id> --wait
```
