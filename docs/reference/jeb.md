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
- Sources with `unmatched_policy = "hold"` do not batch files unless exactly one
  configured Munchy profile route matches and its profile group exists.
- Held capture signatures are stored durably in SQLite and send paced critical
  enrollment reminders, defaulting to once per day, until the signature is
  resolved by config.
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

For incremental source enrollment, set `unmatched_policy = "hold"` on the source
and keep `include_extensions = []` if every file should be considered. Jeb then
compares each eligible file with the configured profile routes before creating a
Munchy batch:

- no matching route: hold the file under a capture signature
- more than one matching route: hold the file until the routes are made
  mutually exclusive
- one matching route with an unknown group: hold the file until the group exists
- one matching route with a known group: include the file in the Munchy batch

Held files stay in the landing directory. Jeb only records signature metadata,
example source-prefixed paths, file counts, byte totals, and mtime bounds in its
state database.

Use `jeb signatures list`, `jeb signatures show <signature-id>`, and
`jeb signatures probe <path>` to inspect held signatures and build new route
rules. `probe` prints the same stable signature that Jeb records, so operators
can test real captures before adding or changing a Munchy profile route.

Route matching supports path-oriented keys such as `path_prefix`, `path_glob`,
`filename_glob`, and `suffixes`. Routes can also require ffprobe metadata with
keys such as `format_name_contains`, `codec_names`, `width`, `height`,
`min_width`, `max_width`, `min_height`, `max_height`, `fps`, `min_fps`,
`max_fps`, `format_tags`, and `stream_tags`.

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
