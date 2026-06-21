# Configuration Reference

## `RIVERHOG_OBJECT_STORE`

- type: enum
- default: `s3`

Selects the committed hot-storage adapter. The active contract is one
S3-compatible object store for committed hot files and incomplete upload
staging.

## `RIVERHOG_LOG_LEVEL`

- type: enum
- default: `INFO`

Minimum Riverhog application log level. Supported values are `CRITICAL`,
`ERROR`, `WARNING`, `INFO`, and `DEBUG`. Planner refreshes and materialization
emit operator-useful phase/progress logs at `INFO`. Successful per-chunk upload
forwarding logs are suppressed at the app boundary so normal `INFO` logs stay
useful during large migrations; failed chunk requests still log.

## `RIVERHOG_S3_ENDPOINT_URL`

- type: URL

Base URL for the S3-compatible object-store API.

## `RIVERHOG_S3_REGION`

- type: string

Region sent to the S3-compatible object-store client.

## `RIVERHOG_S3_BUCKET`

- type: string

Bucket holding both committed hot files and incomplete upload staging.

Committed hot files live at:

```text
collections/{collection_id}/{path}
```

Incomplete staged uploads live at:

```text
.riverhog/uploads/{upload_id}
```

## `RIVERHOG_S3_ACCESS_KEY_ID`

- type: string

Access key used for the S3-compatible object store.

## `RIVERHOG_S3_SECRET_ACCESS_KEY`

- type: secret string

Secret key used for the S3-compatible object store.

## `RIVERHOG_S3_FORCE_PATH_STYLE`

- type: boolean
- default: implementation-defined; `true` for canonical Garage deployments

Enables path-style S3 requests for backends that require them.

## `RIVERHOG_GLACIER_ENDPOINT_URL`

- type: URL
- default: `RIVERHOG_S3_ENDPOINT_URL`

Base URL for the archive-upload object-store API.

## `RIVERHOG_GLACIER_REGION`

- type: string
- default: `RIVERHOG_S3_REGION`

Region sent to the archive-upload object-store client.

## `RIVERHOG_GLACIER_BUCKET`

- type: string
- default: `RIVERHOG_S3_BUCKET`

Bucket holding collection-native Glacier archive packages.

When this differs from `RIVERHOG_S3_BUCKET`, that separate archive bucket must publish
the same abort-incomplete-multipart lifecycle rule as the committed hot-store
bucket.

Collection archive packages are streamed to this bucket. Archives that exceed
the S3 single-object PUT limit, and streamed archives at or above the multipart
part size, use S3 multipart upload with the configured Glacier metadata and
storage class applied at multipart creation time.

## `RIVERHOG_GLACIER_ACCESS_KEY_ID`

- type: string
- default: `RIVERHOG_S3_ACCESS_KEY_ID`

Access key used for Glacier uploads.

## `RIVERHOG_GLACIER_SECRET_ACCESS_KEY`

- type: secret string
- default: `RIVERHOG_S3_SECRET_ACCESS_KEY`

Secret key used for Glacier uploads.

## `RIVERHOG_GLACIER_FORCE_PATH_STYLE`

- type: boolean
- default: `RIVERHOG_S3_FORCE_PATH_STYLE`

Enables path-style requests for Glacier-upload backends that require them.

## `RIVERHOG_GLACIER_PREFIX`

- type: normalized path prefix
- default: `glacier`

New collection Glacier archive packages use opaque archive ids below the
configured prefix:

```text
glacier/archives/{opaque-archive-id}/archive.tar.age
glacier/archives/{opaque-archive-id}/manifest.yml.age
glacier/archives/{opaque-archive-id}/manifest.yml.ots.age
```

The opaque archive id is randomly minted and persisted before upload starts, so
archive multipart retries and app restarts keep using the same object keys
without exposing collection slugs in S3 listings.

The encrypted archive tar contains only the logical collection files and uses the
configured Glacier storage class. Riverhog stores the encrypted collection
manifest and its matching encrypted OpenTimestamps proof as sibling Standard S3
objects so operators can inspect and verify them without restoring Deep Archive
data.

Riverhog also publishes recovery aids under the configured prefix:

```text
glacier/README.md
glacier/catalog/collections.yml.age
```

`README.md` is plaintext, collection-agnostic guidance for recovering data with
standard S3, `age`, and `tar` tools. It contains no collection names. The catalog
is encrypted with the archive passphrase and maps private collection ids to their
opaque archive object paths.

## `RIVERHOG_GLACIER_BACKEND`

- type: string
- default: `s3`

Opaque backend label recorded on collection Glacier summaries.

## `RIVERHOG_GLACIER_STORAGE_CLASS`

- type: string
- default: `DEEP_ARCHIVE`

Intended Glacier storage class recorded on collection Glacier summaries.

## `RIVERHOG_GLACIER_MULTIPART_PART_BYTES`

- type: positive byte count
- default: `64MiB`

Target S3 multipart part size for collection Glacier archive packages. Riverhog
rounds this up when needed to satisfy S3 minimum part size and maximum part
count constraints.

Larger parts reduce request overhead for large migrations. Riverhog persists
the S3 multipart upload id, object key, part size, content length, archive
SHA-256, uploaded part numbers, ETags, sizes, and observed progress in the
catalog while the archive is uploading.
On retry or app restart it lists the uploaded parts for that upload id, skips
the already uploaded contiguous part range in the deterministic archive stream,
uploads the remaining parts, and completes the same multipart upload. S3 keeps
incomplete multipart parts billable until the upload is completed or aborted,
so production buckets should also keep an abort-incomplete-multipart lifecycle
rule as a final cleanup guard.

## `RIVERHOG_GLACIER_MULTIPART_CONCURRENCY`

- type: positive integer
- default: `4`

Maximum number of S3 multipart archive parts Riverhog uploads concurrently for
one collection Glacier archive. Higher values can improve large migration
throughput when the server and network have spare capacity, at the cost of
roughly `part size * concurrency` in buffered archive data plus S3 client
overhead.

Parallel archive uploads still use the persisted multipart state described
above. If an app restart or transient S3 failure interrupts a collection,
Riverhog resumes against the same `UploadId`, verifies the recorded parts that
S3 still has, skips the deterministic contiguous prefix, and uploads any
remaining parts.

## `RIVERHOG_GLACIER_ARCHIVE_ENCRYPTION`

- type: enum, `age_scrypt`
- default: `age_scrypt`

Collection archive packages stored in the archive bucket are always encrypted
with standard binary age v1 scrypt files. The archive object uses the configured
Glacier storage class; the encrypted manifest and proof remain Standard S3
objects. Object metadata records both the stored encrypted object size and the
logical plaintext byte count/SHA-256 used by Riverhog verification. Any value
other than `age_scrypt` is rejected at startup.

Encrypted archive multipart uploads remain resumable. Riverhog persists the age
header/payload nonce as encrypted-upload state with the S3 `UploadId`, then
regenerates exact ciphertext parts from the deterministic archive stream on
retry. The plaintext age file key is not stored in the catalog.

## `RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE`

- type: secret string
- default: `RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE`

Passphrase used for archive package, manifest, proof, and recovery-catalog
encryption. Deployments may share the same passphrase used for disc recovery
payloads or configure a separate archive-only secret.

## `RIVERHOG_GLACIER_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE`

- type: boolean
- default: `false`

When `true`, startup rejects configuration if
`RIVERHOG_GLACIER_ARCHIVE_PASSPHRASE` is missing or still set to the checked-in
development passphrase. Production deployments should set this to `true`.

## `RIVERHOG_GLACIER_ARCHIVE_WORK_FACTOR`

- type: integer, `1..22`
- default: `18`

Scrypt work factor for age scrypt archive-package encryption. This cost is paid
once per collection archive package and once per encrypted manifest/proof. Lower
values are useful for deterministic local integration tests; deployed archives
should use the default unless operational testing shows a clear need to tune it.

## Pinned Hot-File Repair

Collections that are safely archived in Glacier but are not yet protected by
the required verified disc copies remain pinned in hot storage. Riverhog audits
active pins in the background at startup, during Glacier recovery sweeps, and
before planner refreshes. If a pinned hot file is missing or size/checksum
mismatched, Riverhog chooses the least surprising recovery path for that exact
file:

- If registered disc coverage exists, the pin is left waiting for the normal
  `djdan fetch` prompt-based media flow.
- If no registered disc coverage exists for that file, Riverhog automatically
  creates or resumes a collection Glacier restore session, requests the restore,
  polls it, verifies the manifest/proof/archive, and materializes the selected
  files back into hot storage when S3 makes the restored objects readable.

This means Glacier is the only offsite source of truth before physical media
protection exists. There is no separate pre-disc archive store to configure.

## `RIVERHOG_OTS_STAMP_COMMAND`

- type: shell command
- default: `ots`

Command prefix used for production OpenTimestamps proof creation. Riverhog
invokes this command as:

```text
{RIVERHOG_OTS_STAMP_COMMAND} stamp <manifest-path>
```

The command must create `<manifest-path>.ots`.

## `RIVERHOG_OTS_VERIFY_COMMAND`

- type: shell command
- default: `ots`

Command prefix used for production OpenTimestamps proof verification. Riverhog
invokes this command as:

```text
{RIVERHOG_OTS_VERIFY_COMMAND} verify <manifest-path>.ots -f <manifest-path>
```

Collection archive restore and recovery verification first checks the stored
proof object's SHA-256, then runs OpenTimestamps verification against the
expected manifest bytes. Live calendar access remains outside default
deterministic CI.

## `RIVERHOG_RECOVERY_PAYLOAD_COMMAND`

- type: shell command
- default: `age`

Command prefix used for production recovery payload encryption and decryption.
Riverhog invokes the command with the age batchpass plugin:

```text
{RIVERHOG_RECOVERY_PAYLOAD_COMMAND} -e -j batchpass
{RIVERHOG_RECOVERY_PAYLOAD_COMMAND} -d -j batchpass
```

The command must be age 1.3 or newer, or another compatible age command with
`age-plugin-batchpass` available on `PATH`.

## `RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE`

- type: secret string
- default: development-only passphrase

Passphrase supplied to age batchpass through `AGE_PASSPHRASE`. Real deployments
must override the development default and source this value from the deployment's
secret manager.

The checked-in default value is only for local development and deterministic test
harnesses. Do not use `riverhog-dev-recovery-passphrase` for deployed
archives.

## `RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE`

- type: boolean
- default: `false`

When `true`, startup rejects configurations where
`RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE` is missing or still set to the checked-in
development default. Production deployments should set this to `true` and supply
`RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE` explicitly from secrets management. Local and
deterministic harness runs can leave it `false` to keep the checked-in test
passphrase usable.

## `RIVERHOG_RECOVERY_PAYLOAD_WORK_FACTOR`

- type: integer, `1..30`
- default: `12`

Scrypt work factor supplied during encryption through
`AGE_PASSPHRASE_WORK_FACTOR`. Riverhog encrypts each recovery payload file
individually, so this cost is paid many times while materializing discs with
large small-file collections. The default is tuned for high-entropy deployment
passphrases and large optical-image materialization. Higher values are accepted,
but can make planner materialization extremely slow.

## `RIVERHOG_RECOVERY_PAYLOAD_MAX_WORK_FACTOR`

- type: integer, `1..30`
- default: `30`

Maximum accepted scrypt work factor during decryption through
`AGE_PASSPHRASE_MAX_WORK_FACTOR`.

## `RIVERHOG_PLANNER_DISC_TARGET_BYTES`

- type: byte size
- default: `50GB`

Target size for provisional disc images. Byte sizes accept plain bytes and
decimal or binary suffixes such as `50GB`, `500GB`, `46GiB`, and `900MiB`.

## `RIVERHOG_PLANNER_MIN_FILL_RATIO`

- type: ratio or percent
- default: `99%`

Default minimum fill target for ISO-ready planner candidates. Riverhog computes
`RIVERHOG_PLANNER_MIN_FILL_BYTES` from this ratio when an explicit byte value is
not supplied.

## `RIVERHOG_PLANNER_MIN_FILL_BYTES`

- type: byte size
- default: derived from `RIVERHOG_PLANNER_DISC_TARGET_BYTES` and
  `RIVERHOG_PLANNER_MIN_FILL_RATIO`

Explicit minimum ISO size required before a provisional candidate can be
finalized. This must be less than or equal to
`RIVERHOG_PLANNER_DISC_TARGET_BYTES`.

## `RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES`

- type: byte size
- default: `300GB`

Planner-estimated waiting candidate bytes allowed to accumulate before Riverhog
enters saturation splitting. Saturation splitting does not mark underfilled
candidates as ISO-ready. Instead, Riverhog may add extra whole-file voluntary
collection splits, including for collections that already required splitting,
when those splits make an underfilled candidate ready and reduce total waiting
candidate bytes. Required splits and voluntary splits are tracked separately by
the planner; the next saturation split comes from a feasible collection with the
lowest current voluntary split count. Set to `0` to disable saturation
splitting.

## `RIVERHOG_PLANNER_IMAGE_ROOT`

- type: filesystem path
- default: `.riverhog/images`

Directory where Riverhog stores provisional image roots and cached collection
manifest/proof bytes needed for future disc planning. In production this should
be backed by persistent local storage because Glacier manifest and proof objects
may be uploaded directly to archive storage classes.

## `RIVERHOG_UNBURNED_COLLECTION_BYTES_LIMIT`

- type: byte size
- default: `500GB`

Maximum bytes Riverhog will admit as unburned collection data. The count includes
active collection uploads plus committed collection bytes that are not yet
protected by enough registered physical image copies. Set to `0` to disable the
admission cap.

## `RIVERHOG_GLACIER_UPLOAD_RETRY_DELAY`

- type: duration
- default: `5m`

Delay between automatic retry attempts for failed collection archival
finalization work. Riverhog keeps retrying this server-owned stage indefinitely
because packaging, S3 multipart archive upload, hot-file promotion, and final
catalog commit are designed to resume without operator intervention.

## `RIVERHOG_GLACIER_UPLOAD_SWEEP_INTERVAL`

- type: duration
- default: `30s`

How often Riverhog's Glacier-upload worker scans for due collection archive
uploads, retries, and restart-recovered work.

Restart-recovered work resumes one durable job record. For interrupted
collection archive uploads, that job reuses its persisted S3 multipart upload id
when the remote multipart upload still exists. Once S3 has accepted the archive
object, Riverhog uploads and records the sibling manifest/proof objects. A
restart can then resume hot-file promotion without rebuilding or re-uploading
completed Glacier objects.

## `RIVERHOG_PLANNER_REFRESH_SWEEP_INTERVAL`

- type: duration
- default: `60s`

How often Riverhog checks for uploaded collections whose provisional optical
disc candidates are missing, waiting for enough data, failed, or still
materializing after an app restart. Riverhog also queues one planner refresh
immediately on API startup, then continues at this interval. Planner candidate
rows are inserted before materialization begins. Underfilled tail candidates stay
in `waiting` state and do not build an image root until future collections push
them over the minimum fill threshold. Burnable candidates retain partial
encrypted candidate roots under `.candidate-*.tmp` so the next refresh can skip
already completed encrypted files and finish the same candidate id.

## `RIVERHOG_OPERATOR_WEBHOOK_URL`

- type: URL
- default: unset

Single optional operator notification endpoint. Riverhog posts quiet,
operator-facing notifications to this endpoint, including collection ingest
milestones, ready disc-image candidates, persistent archival failures after
retries, verified-copy labeling handoffs, fetch-required handoffs, and Glacier
recovery lifecycle notifications.

The canonical machine-readable event contract is
[`contracts/webhooks/operator-notifications.v1.json`](../../contracts/webhooks/operator-notifications.v1.json).
Each operator webhook includes a `notification` object rendered from that
contract, with canonical `title` and `body` strings intended to be usable
directly by Home Assistant or another receiver. Receivers may still render their
own presentation from the event fields, but the built-in style is quiet:
Riverhog events use `🐷`, djdan/disc-action events use `👨🏻‍🎤`, titles include a
40-character subject, and bodies are brief 150-character status messages.

Collection events are intentionally sparse so long-running retries do not spam
operators. Success milestones include `collections.upload_staged`,
when the full collection has reached server custody, and
`collections.finalized`, when the collection archive is safely uploaded to
Glacier, hot-file promotion has finished, and the collection is available
through Riverhog/WebDAV. Failure/attention events include
`collections.archive_retrying`, paced by
`RIVERHOG_OPERATOR_FAILURE_NOTIFICATION_INTERVAL`, and
`collections.archive_failed` for non-retryable archive validation/data failures,
and `collections.planner_failed`. Archive retry notifications include the
retryable error and next retry time while making clear that Riverhog is still
handling the stage; archive-failed notifications are critical and mean Riverhog
has stopped the automatic retry loop until the problem is inspected. Complete
failed archive uploads are requeued once during app startup so a fixed deployment
can make progress; if that startup retry still hits the same deterministic
failure, Riverhog sends a fresh critical notification so the collection does not
get forgotten. Ready burn candidates use `images.ready`; if
operator reminders are explicitly enabled, repeats use `images.ready.reminder`.
After `djdan` verifies a burned disc but before the operator confirms the
physical label, it triggers `images.copy_label_needed`. If pinned hot files are
missing but registered physical media can recover them, Riverhog emits
`fetches.waiting_media` and then `fetches.waiting_media.reminder` once per
`RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL` while it is still waiting for the
operator to run `djdan fetch`. Glacier recovery is rare and intentionally more
explicit: `glacier_recovery.started` means Riverhog has asked Glacier to
restore archived collection data, `glacier_recovery.ready` means the temporary
restored data is available, `glacier_recovery.ready.reminder` repeats while
operator action or automatic materialization is still incomplete, and
`glacier_recovery.completed` means Riverhog has finished the recovery cleanup.

Webhook delivery is best-effort; Riverhog catalog state remains authoritative.
Notification receivers should keep these operator messages concise and calm:
successful handoffs and burn/recovery readiness are good fits for time-sensitive
mobile notifications, while unrecoverable failure events such as
`collections.planner_failed` should be routed as critical alerts.
Collection payloads include `collection_id`, links back to the collection when
`RIVERHOG_PUBLIC_BASE_URL` is configured, and event-specific progress such as
file counts, staged bytes, archive bytes, object path, or failure details. Ready
image payloads include affected image ids, filenames, download URLs when
`RIVERHOG_PUBLIC_BASE_URL` is configured, and reminder count. Copy-label
payloads include `image_id`, `copy_id`, the exact `label_text`, and an image
link when `RIVERHOG_PUBLIC_BASE_URL` is configured. Fetch-wait payloads include
the fetch id, target, file/byte counts, candidate copy hints, and links to the
fetch summary and manifest. Glacier recovery payloads include the recovery
session id, recovery type, affected images and collections, restore timing, and
precise operator guidance so long bulk restores do not read as data loss.

## `RIVERHOG_OPERATOR_WEBHOOK_TIMEOUT`

- type: duration
- default: `5s`

Outbound timeout for one operator webhook delivery. Collection lifecycle webhook
failures are logged but never block archival finalization. Ready disc-image,
fetch-wait, and Glacier recovery delivery failures are retried by background
workers.

## `RIVERHOG_OPERATOR_WEBHOOK_RETRY_DELAY`

- type: duration
- default: `60s`

Delay before Riverhog retries a failed ready disc-image, fetch-wait, or Glacier
recovery webhook delivery.

With `RIVERHOG_OPERATOR_WEBHOOK_URL` configured, Riverhog rejects startup if
`RIVERHOG_GLACIER_RECOVERY_READY_TTL` is shorter than
`RIVERHOG_OPERATOR_WEBHOOK_TIMEOUT` plus this retry delay.

## `RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL`

- type: duration
- default: `24h`

Interval between repeated ready reminders while a burnable disc-image candidate
is still unfinalized, a fetch manifest is still waiting for `djdan fetch`,
restored collection archive data remains available, or image rebuild staging
data remains available and the recovery session is still incomplete. The
default provides one daily reminder while operator action is still needed; set
this to `0s` to disable ready reminders entirely.

## `RIVERHOG_OPERATOR_FAILURE_NOTIFICATION_INTERVAL`

- type: duration
- default: `24h`

Minimum interval between repeated operator notifications for continuing
retryable background failures on the same durable unit of work. Collection
archival finalization uses this for `collections.archive_retrying` notifications
while continuing to retry on `RIVERHOG_GLACIER_UPLOAD_RETRY_DELAY`.

## `RIVERHOG_GLACIER_RECOVERY_SWEEP_INTERVAL`

- type: duration
- default: `30s`

How often Riverhog scans for due Glacier recovery-session transitions such as
restore-ready and expiry cleanup.

## `RIVERHOG_GLACIER_RECOVERY_RESTORE_LATENCY`

- type: duration
- default: `48h`

Operator-facing restore-latency estimate shown while one requested recovery
session waits for archive restore completion. Real readiness is driven by the
archive object's restore/readability status when a production archive store is
configured.

For the default `DEEP_ARCHIVE` archive storage class and `bulk` retrieval tier,
this default matches AWS's normal S3 Glacier Deep Archive Bulk expectation of
availability within roughly 48 hours. Riverhog treats it as an estimate only:
the recovery reaper still polls S3 `HeadObject` restore state and only marks the
session ready after S3 reports the restored copy is readable.

## `RIVERHOG_GLACIER_RECOVERY_READY_TTL`

- type: duration
- default: `24h`

How long Riverhog keeps restored Standard-storage collection archive data or
rebuilt ISO staging data available after the archive becomes ready before
automatic cleanup expires that recovery session.

For AWS S3 Glacier Flexible Retrieval or Deep Archive objects, Riverhog passes
this value to `RestoreObject` as whole-object restore days, rounded up to avoid
shortening the requested operator window. S3 creates a temporary readable copy
while the underlying object remains in its archive storage class, charges
Standard-storage rates for that temporary copy, and rounds the expiration to the
next midnight UTC after the requested duration. Manifest and OTS proof objects
are stored as Standard S3 siblings and do not require a Glacier restore request.

When `RIVERHOG_OPERATOR_WEBHOOK_URL` is configured, this value must be at least
`RIVERHOG_OPERATOR_WEBHOOK_TIMEOUT` plus
`RIVERHOG_OPERATOR_WEBHOOK_RETRY_DELAY` so one failed ready notification can
still be retried before cleanup.

## `RIVERHOG_GLACIER_RECOVERY_RETRIEVAL_TIER`

- type: enum
- default: `bulk`

Retrieval tier used for recovery-session cost estimates.
For the default `DEEP_ARCHIVE` storage class, `bulk` is the cheapest supported
path and typically completes within 48 hours; `standard` typically completes
within 12 hours and costs more. Expedited retrieval is not exposed because AWS
does not support Expedited retrieval for S3 Glacier Deep Archive objects.

Allowed values:

- `bulk`
- `standard`

## `RIVERHOG_GLACIER_RECOVERY_RESTORE_MODE`

- type: enum
- default: `auto`

Controls how archive restore requests are executed.

Allowed values:

- `auto` — use real archive-object availability; immediately readable S3 objects
  become ready without a fake timer, while AWS archive storage classes use S3
  restore APIs.
- `aws` — always use AWS S3 restore semantics for archived objects.

## `RIVERHOG_TUSD_BASE_URL`

- type: URL

Base URL for the internal `tusd` service that owns resumable staging uploads.
Riverhog remains the public upload contract and maps logical upload resources to
internal `tusd` uploads.

## `RIVERHOG_UPLOAD_STAGING_ROOT`

- type: filesystem path
- default: `.riverhog/uploads`

Directory where Riverhog reads staged upload bytes written by `tusd`.
Production deployments should mount the same persistent local filesystem path
into both `tusd` and `riverhog-app`. The checked-in compose stack mounts this
directory as `/uploads` in both containers.

This directory is temporary ingest custody, not authoritative archive storage.
Riverhog deletes staged files only after the collection archive is safely
uploaded and verified in Glacier-compatible storage, the collection files are
promoted into committed hot storage, or the upload session is canceled or
expired.

## CLI upload transport

The following variables are read by the `riverhog` CLI process. They tune the
public client-to-API upload path, not the internal API-to-tusd forwarding path.
See [Upload Transport Reference](upload-transport.md) before increasing these
values on a new network path.

### `RIVERHOG_UPLOAD_CHUNK_BYTES`

- type: positive integer byte count
- default: `8388608`

Size of each tus-compatible `PATCH` request body sent by `riverhog collection upload`.
Reverse proxies must allow bodies larger than this value. A proxy body limit of
at least 16 MiB is recommended for the default 8 MiB chunk.

### `RIVERHOG_UPLOAD_FILE_CONCURRENCY`

- type: positive integer
- default: `1`

Maximum number of logical files one `riverhog collection upload` process uploads at the
same time. Each worker uses its own API client and resumes exactly one
server-owned file upload resource at a time. Keep the default for large-file
collections when a single stream already fills the path; raise it for
collections with many small files where per-file round trips dominate.

### `RIVERHOG_UPLOAD_FILE_LOG_BYTES`

- type: non-negative integer byte count
- default: `1048576`

Minimum logical file size that gets per-file start/completion log lines during
`riverhog collection upload`. Smaller files still contribute to throttled total progress
logs, and retries/errors always name the affected path. Set this to `0` for
full per-file logging while debugging a small collection.

### `RIVERHOG_UPLOAD_TIMEOUT_SECONDS`

- type: positive number of seconds
- default: `300`

Per-chunk client timeout for the public upload `PATCH`. Proxy request-body and
proxy send/read timeouts should be slightly higher than this value so abandoned
chunk bodies are cleaned up promptly without racing normal uploads.

### `RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS`

- type: positive number of seconds
- default: `5`

Polling interval after all collection files are staged. The CLI keeps waiting
until the collection is finalized, which means the Glacier archive package has
uploaded, verified, hot files have been promoted, and the planner refresh has
been requested. While waiting, the CLI reports archive phase, multipart progress,
and hot-promotion progress when the server has those counters.

### `RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS`

- type: non-negative number of seconds
- default: unset

Maximum time the CLI waits for collection finalization after all files are
staged. Unset or `0` means wait indefinitely. If this timeout is reached,
`riverhog collection upload` prints the current upload session and exits non-zero instead
of presenting staged bytes as a completed collection.

### `RIVERHOG_DOWNLOAD_TIMEOUT_SECONDS`

- type: positive number of seconds
- default: `3600`

Client timeout for large Riverhog downloads such as finalized or restored ISO
images. The server may spend several minutes preparing ISO metadata before the
first response bytes arrive, especially for images with many small files.
Reverse-proxy read timeouts for ISO download routes should be at least this
large or explicitly chosen to bound that preparation window.

### `RIVERHOG_COPY_REGISTRATION_TIMEOUT_SECONDS`

- type: positive number of seconds
- default: `3600`

Client timeout for physical copy registration and copy-state updates. `djdan
burn` uses this after media verification and label confirmation while Riverhog
records the generated copy id, storage location, and per-file recovery index for
the image. Images with many small files can take noticeably longer than ordinary
API calls, so the default is intentionally long enough for normal operation.

### `RIVERHOG_HTTP2`

- type: boolean
- default: `true`

Whether the CLI attempts HTTP/2 for HTTPS API requests. Uploads must work over
HTTP/1.1 and HTTP/2 when chunk sizing, proxy limits, and timeouts are correct.

### `RIVERHOG_UPLOAD_BASE_URL`

- type: URL
- default: unset

Optional scheme and host override for absolute upload URLs returned by the API.
The API-provided path is preserved. This is useful when upload traffic is sent
through a tunnel or a LAN address while normal API URLs remain public.

### `RIVERHOG_HOST_HEADER`

- type: string
- default: unset

Optional `Host` header override for CLI requests, useful when connecting to a
specific LAN IP behind a name-based reverse proxy.

### `RIVERHOG_TLS_VERIFY`

- type: boolean
- default: `true`

Whether the CLI verifies TLS certificates.

## `RIVERHOG_TUSD_HOOK_SECRET`

- type: secret string

Shared secret used to authenticate `tusd` hook callbacks. Hooks are
notifications only; Riverhog's catalog state remains authoritative.

## `RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS`

- type: positive number of seconds
- default: `60`

Maximum time Riverhog will wait while forwarding one chunk to the internal
`tusd` service. If the backend stalls longer than this, Riverhog returns a
transient 503 so upload clients can re-check the resumable offset and retry
instead of leaving a long-running server-side PATCH behind.

## `RIVERHOG_WEBDAV_ENABLED`

- type: boolean
- default: `false`

Enables the supported read-only WebDAV browsing surface for committed hot files.

## `RIVERHOG_WEBDAV_ADDR`

- type: address
- default: `127.0.0.1:8080`

Bind address for the read-only WebDAV sidecar when that surface is enabled.
WebDAV must expose only the committed `collections/` namespace and must not
expose `.riverhog/` staging paths.

## `RIVERHOG_DATABASE_URL`

- type: PostgreSQL SQLAlchemy database URL
- default: `postgresql+psycopg://riverhog:riverhog@127.0.0.1:5432/riverhog`

This is the catalog database URL used for durable authoritative API state. The
checked-in Compose stack sets this to the Postgres sidecar at `postgres:5432`.
SQLite/local path catalogs are no longer supported; `RIVERHOG_DB_PATH` is
rejected at startup so stale local-development env files fail loudly.

## `RIVERHOG_PUBLIC_BASE_URL`

- type: URL
- default: unset

Optional public API base URL used when Riverhog builds webhook links back to
collection uploads, fetch manifests, collection restore sessions, image rebuild
sessions, and finalized-image ISO downloads.

## `INCOMPLETE_UPLOAD_TTL`

- type: duration
- default: `24h`

This controls how long incomplete server-side upload state for one collection-upload file or one fetch-manifest entry
may remain resumable after the last successfully accepted chunk.

Service restart does not shorten this TTL or discard unexpired upload state by itself.

When the TTL expires:

- for collection ingest, the staged upload is deleted and that file returns to `pending`
- the pending `tusd` upload is cancelled
- any incomplete staged recovery upload is deleted
- the fetch entry returns to `pending`
- the fetch manifest returns to `waiting_media` if any selected bytes are still not hot
- `upload_state_expires_at` becomes `null` until a new upload session is opened

## `RIVERHOG_UPLOAD_SESSION_IDLE_TTL`

- type: duration
- default: `168h`

This controls how long an open incremental collection upload session may remain
idle before Riverhog expires it. Activity includes opening or resuming the
session, registering a file, creating or resuming a file upload resource,
accepting upload chunks, and completing or canceling the session.

When an open session is idle past this TTL:

- outstanding `tusd` upload resources are canceled
- staged upload bytes are deleted
- registered file rows are removed
- the upload session remains as a closed `expired` audit record

This TTL is intentionally separate from `INCOMPLETE_UPLOAD_TTL`. A collection
session may reasonably span multiple operator runs, while an individual partial
file upload should expire more quickly after its last accepted chunk.

## `UPLOAD_EXPIRY_SWEEP_INTERVAL`

- type: duration
- default: `30s`

This controls how often Riverhog's background expiry reaper sweeps collection-upload and fetch-upload state looking
for entries whose published `INCOMPLETE_UPLOAD_TTL` has already elapsed.

Lower values reduce how long expired upload state may remain present after its TTL boundary. Higher values reduce
background sweep frequency at the cost of slower cleanup after expiry.
