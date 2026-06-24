# Jeb

`jeb` is a watched-drop collector for weekly device batches. It watches one or
more landing directories, creates one durable collection batch when the schedule
and threshold allow it, and submits that structured upload to Munchy.

Jeb is generic public code. Real source names, credentials, hostnames, webhook
URLs, and deployment overlays belong outside this repository.

## Behavior

- Each collection has at most one active batch.
- Files must be stable before they are eligible.
- Weekly collections include files older than the current scheduled boundary.
- Source `upload_prefix` values are preserved in the Munchy upload paths.
- Batch state is stored in SQLite with WAL enabled.
- Service restarts resume from the current batch state.
- Transient target and network errors retry forever without operator webhook noise.
- Unrecoverable errors move the batch to `failed_notified` and send a critical
  operator webhook.
- `failed_notified` batches remain active and send a paced critical reminder,
  defaulting to once per day, until the operator resolves them.
- Before batching a source, Jeb sends the source-prefixed file tree and
  ffprobe-derived routing summaries to Munchy profile-routing preflight.
- If any source file falls through the ordered Munchy routes, the whole source
  is skipped and Jeb records a durable routing-preflight failure.
- Routing-preflight failures send critical operator webhooks with paced daily
  reminders. Scheduled runs do not retry that source until the operator runs
  `jeb archive-now --source <source-id>` and the Munchy preflight passes.
- Source files are deleted only after Munchy reports the job is safe to delete
  and the collection uses `cleanup = "after_target_success"`.

## Munchy Routing

Jeb does not choose encode profiles. It sends source-prefixed paths, file facts,
and ffprobe-derived routing summaries to Munchy preflight, then includes the
same configured `profile_routing` rules in the Munchy job request.

Route rules are ordered by priority. The first route that matches a file wins,
and a file that falls through the full route list fails preflight. Broad routes
are allowed because they are explicit ordered matchers. Use path prefixes,
suffixes, and ffprobe-style metadata matches in Munchy when the source
directory alone is not enough to choose a profile.

Use a `passthrough` profile group for any recurring weekly artifact that should
be copied into the collection without GPU work.

For incremental source enrollment, keep `include_extensions = []` if every file
should be considered. Jeb asks Munchy to preflight the whole pending source set
before it creates a Munchy batch:

- every file has a winning route and a known group: create the batch
- any file falls through the route list: record a durable routing-preflight
  failure and skip the source
- Munchy route config is invalid: record a durable routing-preflight failure and
  skip the source

After fixing routes, run `jeb archive-now --source <source-id>`. The command
retries preflight immediately, clears the durable failure only if Munchy accepts
the full source set, and starts an archive attempt for the source.

Route matching supports path-oriented keys such as `path_prefix`, `path_glob`,
`filename_glob`, and `suffixes`. Routes can also require ffprobe metadata with
keys such as `format_name_contains`, `codec_names`, `width`, `height`,
`min_width`, `max_width`, `min_height`, `max_height`, `duration`,
`min_duration`, `max_duration`, `fps`, `min_fps`, `max_fps`,
`video_codec_names`, `audio_codec_names`, `has_audio`, `has_video`,
`video_stream_count`, `audio_stream_count`, `format_tags`, `video_tags`, and
`audio_tags`.

## Webhooks

`jeb` uses the Riverhog operator webhook contract with:

- `source = "jeb"`
- `actor = "jeb"`
- emoji `🤖`
- event `jeb.issue`

Only unrecoverable or operator-blocking failures send webhooks. Transient upload
or target errors retry silently.

See [`config/examples/jeb/jeb.toml`](../../config/examples/jeb/jeb.toml) for the
generic configuration shape.
