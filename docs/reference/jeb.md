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
- Source files are deleted only after Munchy reports the job is safe to delete
  and the collection uses `cleanup = "after_target_success"`.

## Munchy Routing

Jeb does not choose encode profiles. It sends source-prefixed paths to Munchy and
includes the configured `profile_routing` rules in the Munchy job request.

Route rules should be mutually exclusive. Use path prefixes, suffixes, and
ffprobe-style metadata matches in Munchy when the source directory alone is not
enough to choose a profile.

Use a `passthrough` profile group for any recurring weekly artifact that should
be copied into the collection without GPU work.

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
