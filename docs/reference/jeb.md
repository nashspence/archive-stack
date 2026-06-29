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
- Before batching a source, Jeb sends the source-prefixed file tree plus
  path, ffprobe, and ExifTool routing facts to Munchy profile-routing
  preflight.
- If any source file falls through the ordered Munchy routes, the whole source
  is skipped and Jeb records a durable profile-routing failure.
- Transient Munchy preflight transport or server failures are logged and retried
  later. Non-transient Munchy API or contract failures are recorded as durable
  `munchy_preflight` failures, not as unmatched media.
- Routing-preflight failures send critical operator webhooks with paced daily
  reminders. Scheduled runs do not retry that source until the operator runs
  `jeb archive-now --source <source-id>` and the Munchy preflight passes.
- Source files are deleted only after Munchy reports the job is safe to delete
  and the collection uses `cleanup = "after_target_success"`.

## Munchy Routing

Jeb does not choose encode profiles. It sends source-prefixed paths and
normalized routing facts to Munchy preflight, then includes the same configured
`profile_routing` rules in the Munchy job request.

Route rules are ordered by priority. The first route that matches a file wins,
and a file that falls through the full route list fails preflight. Broad routes
are allowed because they are explicit ordered matchers. A broad final route is
just a route with `when = {}`. Falling through every route remains a preflight
failure.

Routes use one predicate shape:

```toml
[[munchy_job_defaults.profile_routing.routes]]
id = "example-video"
group = "video"
into = "camera/video"
when = { path = { prefix = "camera", suffix_in = [".mp4", ".mov"] } }
```

Predicates support `all`, `any`, `not`, `path`, `fact`, `gate`, and `pair`.
Useful facts include `path.*`, `video.*`, `audio.*`, `ffprobe.*`, and
`exif.*`. For example:

```toml
[[munchy_job_defaults.profile_routing.routes]]
id = "iphone-se2-4k60-hevc"
group = "iphone-video"
into = "iphone-se2/video-4k60"
when = { all = [
  { gate = "iphone-se2-native-camera" },
  { path = { suffix = ".mov" } },
  { fact = "video.resolution", equals = "4k" },
  { fact = "video.fps", equals = 60 },
  { fact = "video.codec", equals = "hevc" },
] }
```

`pairings` run before route matching. Use them for multi-file captures such as
Live Photos so the paired movie is not accidentally classified as a normal
video.

`action = "leave"` marks a matched file as intentionally kept in Jeb custody
instead of uploaded in that batch. Use it for known but currently unarchived
inputs such as a downloads directory. Files that do not match any route are not
left behind silently; they fail preflight.

Use an `originals` profile group for any recurring weekly artifact that should
be copied into the collection without GPU work.

For incremental source enrollment, keep `include_extensions = []` if every file
should be considered. Jeb asks Munchy to preflight the whole pending source set
before it creates a Munchy batch:

- every file has a winning upload route and a known group: create the batch
- a file has a winning `leave` route: keep it in the source directory and omit
  it from the batch
- any file falls through the route list: record a durable profile-routing
  failure and skip the source
- transient Munchy preflight transport or server failure: log and try again on a
  later scheduled run
- non-transient Munchy API or contract failure: record a durable
  `munchy_preflight` failure and skip the source

After fixing routes, run `jeb archive-now --source <source-id>`. The command
retries preflight immediately, clears the durable failure only if Munchy accepts
the full source set, and starts an archive attempt for the source. Use the same
command after repairing a non-transient Munchy preflight API failure.

## Webhooks

`jeb` uses the Riverhog operator webhook contract with:

- `source = "jeb"`
- `actor = "jeb"`
- emoji `🤖`
- event `jeb.issue`

Only unrecoverable or operator-blocking failures send webhooks. Transient upload
or target errors retry silently.

See [`config/examples/jeb/jeb.toml`](../../config/examples/jeb/jeb.toml) for the
generic configuration shape. See [`munchy.md`](munchy.md) for Munchy metadata
projection and XMP sidecar behavior.
