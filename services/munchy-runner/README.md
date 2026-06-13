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
- upload finished archive collection files through Riverhog upload sessions when configured
- retry handoffs until success
- clean local source spools and scratch data according to configured TTLs

Riverhog remains the custody boundary. Munchy produces finished collections and
hands them off; Riverhog does not orchestrate device offload or GPU transcodes.

## Runtime

The compose example runs:

- `munchy-runner` on `127.0.0.1:8092`
- `munchy-runner-tusd` on `127.0.0.1:8093/files`
- `munchy-runner-lan-gateway` as an optional nginx gateway

Inspect active work with the Munchy CLI:

```bash
munchy job list --runner-url http://127.0.0.1:8092
munchy job watch <job-id> --runner-url http://127.0.0.1:8092
munchy job cancel <job-id> --runner-url http://127.0.0.1:8092 --cleanup --yes
```

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
- `MUNCHY_RUNNER_RIVERHOG_WAIT`
- `MUNCHY_RUNNER_RIVERHOG_UPLOAD_CHUNK_BYTES`
- `MUNCHY_RUNNER_RIVERHOG_UPLOAD_HTTP2`
- `MUNCHY_RUNNER_RIVERHOG_UPLOAD_WRITE_CHUNK_BYTES`
- `MUNCHY_RUNNER_RIVERHOG_UPLOAD_WRITE_DELAY_SECONDS`
- `MUNCHY_RUNNER_NOTIFY_ENABLED`
- `MUNCHY_RUNNER_NOTIFY_WEBHOOKS`
- `MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS`
- `MUNCHY_RUNNER_NOTIFY_DEFAULT_ENABLED`
- `MUNCHY_RUNNER_NOTIFY_UPLOAD_WAITING_REMINDER_SECONDS`
- `MUNCHY_RUNNER_MAX_ACTIVE_INPUT_UPLOADS`
- `MUNCHY_RUNNER_MAX_RUNNING_JOBS`
- `MUNCHY_RUNNER_STORAGE_WAIT_SECONDS`

Archive-only profile groups are encoded eagerly as soon as files are uploaded.
`MUNCHY_RUNNER_EAGER_ARCHIVE_PIPELINE_BATCHES` controls how many eager archive
batches may be queued/running on the GPU target at once; the target still owns
the actual encode concurrency limit. The default is `3`, which keeps one batch
ready behind the active work to avoid starving the encoder during batch
transitions.

Job admission and job execution are separate. Valid jobs are accepted as queued
work, while `MUNCHY_RUNNER_MAX_RUNNING_JOBS` controls how many jobs the runner
starts at once. The default is `1` because one collection job is expected to
fully use the encoder. New input uploads are still admitted only when the upload
buffer and future scratch reservation fit. Already-admitted uploads and jobs
retry transient network, capacity, and scratch-space pressure instead of failing
immediately.

Webhook payloads identify themselves with `source = "munchy"` and the canonical
emoji `🤤`.
When runner notifications are enabled, `MUNCHY_RUNNER_NOTIFY_DEFAULT_RECIPIENTS`
can provide comma-separated recipients for jobs whose request omits an explicit
`notify` block.
Incomplete uploads that appear stalled while already-uploaded files have been
encoded emit `job.upload_waiting.reminder` at most once per
`MUNCHY_RUNNER_NOTIFY_UPLOAD_WAITING_REMINDER_SECONDS`.

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

## Archive Handoff Lifecycle

Archive jobs may enable Riverhog upload with:

```json
{"riverhog": {"enabled": true, "wait": "staged"}}
```

When enabled, the runner opens or resumes a Riverhog collection-upload session
and uploads finished archive artifacts through Riverhog's session file-upload
API. `wait = "staged"` waits until all session files have been accepted and the
session has been completed. `wait = "finalized"` also waits for Riverhog to make
the collection visible as finalized.

Archive-only profile groups are encoded eagerly as uploaded source files become
complete. This lets the runner overlap source upload, GPU encode work, and
Riverhog upload while keeping local storage pressure low.

## Cleanup Contract

The runner owns pre-custody local storage:

- source upload bytes in the TUS spool
- shared input trees used by the GPU target
- GPU job scratch directories
- encoded archive artifacts waiting for Riverhog
- review artifacts waiting for the configured review upload

For archive-only eager groups, the runner consumes a source upload file after
that file has been successfully encoded. For Riverhog-enabled archive jobs, the
runner removes an encoded artifact after Riverhog confirms the corresponding
session file is uploaded.

Successful terminal cleanup removes remaining local job work and input-upload
state when the workflow no longer needs it. Cancellation requests cancel the
Riverhog upload session when one exists, then clean local runner work and the
referenced input upload. Unrecoverable encode failures write a debug bundle
before cleanup so the operator can inspect the failed job without keeping the
full source spool around.
