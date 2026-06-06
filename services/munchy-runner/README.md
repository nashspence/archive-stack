# Munchy Runner

`munchy-runner` is the non-GPU orchestration service for Munchy media ingest. It
owns pre-custody work until a finished archival collection is handed to
Riverhog.

The runner is intentionally generic:

- no deployment-specific hostnames
- no private device names
- no private rclone destinations
- no operator webhook configuration

Private deployment configuration supplies source directories, profile-group
mappings, review destinations, webhook recipients, Riverhog credentials, and
GPU service-manager details.

## Responsibilities

- receive resumable source uploads through TUS
- require uploaded paths shaped as `<profile-group>/<file>`
- map profile groups to Munchy encode profiles
- persist uploads, jobs, retry state, and notification state in SQLite
- preflight disk usage before accepting work
- submit GPU encode work to `munchy-av1-nvenc`
- upload review outputs when configured
- call `riverhog upload` for finished archive collections when configured
- retry handoffs until success
- clean local source spools and scratch data according to configured TTLs

Riverhog remains the custody boundary. Munchy produces finished collections and
hands them off; Riverhog does not orchestrate device offload or GPU transcodes.

## Runtime

The compose example runs:

- `munchy-runner` on `127.0.0.1:8092`
- `munchy-runner-tusd` on `127.0.0.1:8093/files`
- `munchy-runner-lan-gateway` as an optional nginx gateway

Important environment variables:

- `MUNCHY_RUNNER_STATE_DIR`
- `MUNCHY_RUNNER_WORK_DIR`
- `MUNCHY_RUNNER_TUSD_DIR`
- `MUNCHY_RUNNER_GPU_RUNTIME_DIR`
- `MUNCHY_RUNNER_GPU_MANAGER_URL`
- `MUNCHY_RUNNER_GPU_TARGET_URL`
- `MUNCHY_RUNNER_MIN_FREE_BYTES`
- `MUNCHY_RUNNER_EAGER_ARCHIVE_PIPELINE_BATCHES`
- `MUNCHY_RUNNER_REVIEW_UPLOAD_ENABLED`
- `MUNCHY_RUNNER_RIVERHOG_UPLOAD_ENABLED`
- `MUNCHY_RUNNER_NOTIFY_ENABLED`
- `MUNCHY_RUNNER_NOTIFY_WEBHOOKS`
- `MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS`
- `MUNCHY_RUNNER_NOTIFY_DEFAULT_ENABLED`

Archive-only profile groups are encoded eagerly as soon as files are uploaded.
`MUNCHY_RUNNER_EAGER_ARCHIVE_PIPELINE_BATCHES` controls how many eager archive
batches may be queued/running on the GPU target at once; the target still owns
the actual encode concurrency limit. The default is `3`, which keeps one batch
ready behind the active work to avoid starving the encoder during batch
transitions.

Webhook payloads identify themselves with `source = "munchy"` and the canonical
emoji `🤤`.
When runner notifications are enabled, `MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS`
can provide comma-separated recipients for jobs whose request omits an explicit
`notify` block.

## Upload Shape

Every source file belongs to a profile group:

```text
<profile-group>/<file>
```

For example:

```text
main-camera/C0001.MP4
slow-motion/C0002.MP4
front-sensor/recording.webm
```

The runner rejects ambiguous uploads that omit the profile-group directory.
