# Jeb

`jeb` is an env-configured account scheduler for watched-drop archive uploads.
It watches one landing directory per account, starts one Munchy
`collection_archive` job per account when files are eligible, and tells Munchy
to upload the finished archive to Riverhog.

Jeb is generic public code. Real account names, credentials, hostnames, webhook
URLs, and deployment overlays belong outside this repository.

## Account Model

One account slug is the identity everywhere:

```text
account name == landing directory == Munchy profile group == Riverhog collection slug
```

For account `example-camera`, Jeb watches:

```text
/landing/example-camera/
```

and uploads files to Munchy under:

```text
example-camera/<relative-file>
```

The Munchy runner infers the profile group from that first path segment. Jeb
does not carry device metadata, route tables, or encode profiles.

## Munchy Boundary

Jeb deliberately treats Munchy routing as a target-owned concern. During
discovery, Jeb may ask Munchy to preflight the complete eligible file set for an
account. That preflight is only a go/no-go gate: if Munchy reports that the job
configuration cannot handle the set, Jeb stops before upload and notifies the
operator. If Munchy accepts the set, Jeb uploads every eligible file in the
batch.

Jeb must not interpret Munchy's route plan, `leave` results, route ids, profile
groups, or culling decisions as instructions for which source files to upload or
delete. Munchy owns routing, metadata projection, preserve/encode/leave/cull
semantics, and Riverhog archive contents. After Munchy reports the configured
safe Riverhog success state, Jeb cleanup applies to the complete source batch it
uploaded.

To add an account, provision the matching drop account/directory, add the slug
to `JEB_ACCOUNTS`, and make sure Munchy has the expected profile behavior for
that group.

## Behavior

- Each account has at most one active scheduled batch.
- Files must be stable before they are eligible.
- Scheduled cadences include files older than the current scheduled boundary.
- `seasonal` uses Dec 1, Mar 1, Jun 1, and Sep 1 season boundaries and runs on
  the first configured weekly slot after each boundary.
- Batch state is stored in SQLite with WAL enabled.
- Service restarts resume from the current batch state.
- Transient target and network errors retry without operator webhook noise.
- Unrecoverable errors move the batch to `failed_notified` and send a critical
  operator webhook when notifications are enabled.
- Source files are deleted only after Munchy reports the job is safe to delete
  and `JEB_CLEANUP=after_target_success`.

## Environment

Required:

```sh
JEB_ACCOUNTS=example-camera,example-phone
JEB_MUNCHY_URL=http://munchy-runner:8080
```

Common settings:

```sh
JEB_LANDING_DIR=/landing
JEB_STATE_DIR=/state
JEB_CADENCE=weekly
JEB_WEEKDAY=monday
JEB_HOUR=3
JEB_MINUTE=0
JEB_STABLE_AGE=10m
JEB_INCLUDE_EXTENSIONS=.mp4,.mov,.mkv,.webm,.xml,.json,.txt
JEB_ARCHIVE_TASKS=archive_video
JEB_CLEANUP=after_target_success
JEB_RIVERHOG_WAIT=finalized
```

Supported cadences are:

- `weekly`: once per configured weekday/time.
- `monthly`: first configured weekday/time on or after the first day of each
  month.
- `seasonal`: first configured weekday/time on or after Dec 1, Mar 1, Jun 1,
  and Sep 1.
- `manual`: no scheduled batch; use `jeb archive-now --account <slug>`.

Per-account cadence overrides use the env-safe account slug:

```sh
JEB_ACCOUNT_EXAMPLE_PHONE_CADENCE=seasonal
```

See [`config/examples/jeb/jeb.env`](../../config/examples/jeb/jeb.env) for a
complete generic env example.

## Commands

```sh
jeb check-config
jeb once
jeb run
jeb archive-now --account example-camera
```

`archive-now` starts an immediate account batch for currently eligible files.
Use `--no-process` to create the batch without processing it in the same command.

## Health

`jeb run` exposes HTTP health endpoints for container orchestration:

- `/health/live` reports that the process is serving health requests.
- `/health/ready` reports readiness after configuration loads and the state
  database initializes.

## Webhooks

`jeb` uses the Riverhog operator webhook contract with:

- `source = "jeb"`
- `actor = "jeb"`
- event `jeb.issue`

Only unrecoverable or operator-blocking failures send webhooks. Transient upload
or target errors retry silently.
