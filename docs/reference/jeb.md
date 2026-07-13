# Jeb

Jeb is an account scheduler for watched-drop archive uploads. The service
runner watches one landing directory per account, starts one Munchy
`collection_archive` job per account when files are eligible, and tells Munchy
to upload the finished archive to Riverhog. The public `jeb` command is the
remote operator CLI; service-local collector execution uses `jeb-service`.

Jeb is generic public code. Real account names, credentials, hostnames, webhook
URLs, and deployment overlays belong outside this repository.

## Account Model

The account slug is Jeb's stable identity for a source. For account
`example-camera`, Jeb watches:

```text
/landing/example-camera/
```

and uploads files to Munchy under the same account-rooted relative path:

```text
example-camera/<relative-file>
```

That upload root is intentionally not configurable. If a device needs richer
routing, route ids, groups, encode profiles, metadata projection, or culling
behavior, those details live in a Munchy job config file. Jeb can load one
explicit file from `JEB_ACCOUNT_<ACCOUNT>_MUNCHY_CONFIG`, or one mounted config
directory from `JEB_MUNCHY_CONFIG_DIR` where each account has
`<account>.munchy.yaml`. The file uses the same public `munchy.job` schema as
`munchy job start --config`; Jeb lowers it through Munchy authoring code and
does not treat it as Jeb routing policy.

## Munchy Boundary

Jeb deliberately treats Munchy routing as a target-owned concern. During
discovery, Jeb may ask Munchy to preflight the complete eligible file set for an
account using the configured Munchy job. That preflight is only a go/no-go gate:
if Munchy reports that the job configuration cannot handle the set, Jeb stops
before upload and notifies the operator. If Munchy accepts the set, Jeb uploads
every eligible file in the batch.

Jeb must not interpret Munchy's route plan, `leave` results, route ids, profile
groups, or culling decisions as instructions for which source files to upload or
delete. Munchy owns routing, metadata projection, preserve/encode/leave/cull
semantics, and Riverhog archive contents. After Munchy reports the configured
safe Riverhog success state, Jeb cleanup applies to the complete source batch it
uploaded.

Jeb does own the lifecycle contract for jobs it submits. It starts Munchy jobs
with `riverhog_upload_session_on_failure: cancel`, so a terminal Munchy failure
does not leave an open Riverhog upload session behind for an automation attempt
that Jeb has already marked failed.

This boundary keeps Jeb as a small account scheduler and uploader while keeping
the media-specific rules in Munchy, where the same routing config can be used by
other clients. Disk-backed Munchy config can also instantiate reusable public
`munchy.device_profile` files; both account configs and device profiles are
structured, reviewed as files, mounted read-only, and validated with normal YAML
config errors.

To add an account, provision the matching drop account/directory, add the slug
to `JEB_ACCOUNTS`, and either add `<account>.munchy.yaml` under
`JEB_MUNCHY_CONFIG_DIR` or point `JEB_ACCOUNT_<ACCOUNT>_MUNCHY_CONFIG` at the
account's Munchy config when the default collection-archive job is not precise
enough.

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
JEB_MUNCHY_CONFIG_DIR=/config/jeb
```

Supported cadences are:

- `weekly`: once per configured weekday/time.
- `monthly`: first configured weekday/time on or after the first day of each
  month.
- `seasonal`: first configured weekday/time on or after Dec 1, Mar 1, Jun 1,
  and Sep 1.
- `manual`: no scheduled batch; use `jeb archive-now --account <slug>` from an
  operator client, or `jeb-service archive-now --account <slug>` inside the
  service environment.

Per-account cadence overrides use the env-safe account slug:

```sh
JEB_ACCOUNT_EXAMPLE_PHONE_CADENCE=seasonal
```

See [`config/examples/jeb/jeb.env`](../../config/examples/jeb/jeb.env) for a
complete generic env example.

## Operator CLI

`jeb` talks to the authenticated Riverhog API using the same `RIVERHOG_BASE_URL`,
`RIVERHOG_TOKEN`, TLS, and host-header conventions as `riverhog`.

```sh
jeb check-config
jeb status
jeb batches
jeb once
jeb archive-now --account example-camera
```

`archive-now` starts an immediate account batch for currently eligible files and
returns after the deployed Jeb service accepts the operation. Use `batches` or
`status` to follow the resulting work. Use `--no-process` to create the batch
without processing it in the same command. Use `--dry-run` to preview the exact
archive action without creating a batch, uploading files, starting a runner job,
or recording routing preflight failures. `--no-process` is still a mutating
staged-batch operation; combine it with `--dry-run` only when previewing that
staged-batch plan.

`status` is read-only and summarizes configured accounts, eligible source
backlog, batch state counts, active attempts, recent failures, and routing
preflight failures. Use `--json` for compact machine-readable output, or
`--no-backlog` to skip source directory scans.

`batches` is read-only and lists batch attempts, which are the operational
records that move through active, retry, failure, and cleanup states. It follows
the Riverhog CLI family paging shape:

```sh
jeb batches --page 1 --per-page 25 --sort updated_at --order desc
jeb batches --terminal all --account example-camera --json
jeb batches --state cleanup_failed --query failed
```

Supported batch filters include `--terminal active|terminal|all`, `--state`,
`--account`, `--collection`, `--target`, and `--query`/`-q`. Supported sort
fields are `updated_at`, `created_at`, `collection`, `collection_timestamp`,
`target`, `state`, `file_count`, `bytes`, `attempt`, and `job_id`.

## Service CLI

`jeb-service` is the env-configured collector CLI used by the long-running Jeb
service container. It requires the `JEB_*`, notification, Munchy, landing, and
state environment for that service. Do not deploy it as a normal remote client
command.

```sh
jeb-service check-config
jeb-service status
jeb-service batches
jeb-service once
jeb-service run
jeb-service archive-now --account example-camera
```

## API And Health

Riverhog exposes the authenticated remote API under:

- `GET /v1/jeb/status`
- `GET /v1/jeb/batches`
- `GET /v1/jeb/config/check`
- `POST /v1/jeb/once`
- `POST /v1/jeb/archive-now`

`jeb-service run` exposes internal service endpoints for Riverhog and container
orchestration:

- `/health/live` reports that the process is serving health requests.
- `/health/ready` reports readiness after configuration loads and the state
  database initializes.
- `/v1/jeb/...` is the internal Jeb service API proxied by Riverhog's public API.

## Webhooks

`jeb` uses the Riverhog operator webhook contract with:

- `source = "jeb"`
- `actor = "jeb"`
- event `jeb.issue`

Only unrecoverable or operator-blocking failures send webhooks. Transient upload
or target errors retry silently.
