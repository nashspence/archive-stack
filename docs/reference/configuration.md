# Configuration Reference

## `RIVERHOG_OBJECT_STORE`

- type: enum
- default: `s3`

Selects the committed hot-storage adapter. The active contract is one
S3-compatible object store for committed hot files and incomplete upload
staging.

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

Collection Glacier archive packages use privacy-safe keys below the configured prefix:

```text
glacier/collections/{collection_id_hash}/archive.tar
glacier/collections/{collection_id_hash}/manifest.yml
glacier/collections/{collection_id_hash}/manifest.yml.ots
```

The hash segment is derived from the canonical collection id. These keys must
not embed raw collection ids or logical file paths.

## `RIVERHOG_GLACIER_BACKEND`

- type: string
- default: `s3`

Opaque backend label recorded on collection Glacier summaries.

## `RIVERHOG_GLACIER_STORAGE_CLASS`

- type: string
- default: `DEEP_ARCHIVE`

Intended Glacier storage class recorded on collection Glacier summaries.

## `RIVERHOG_OTS_STAMP_COMMAND`

- type: shell command
- default: `ots`

Command prefix used for production OpenTimestamps proof creation. Riverhog
invokes this command as:

```text
{RIVERHOG_OTS_STAMP_COMMAND} stamp <manifest-path>
```

The command must create `<manifest-path>.ots`. Live external anchoring coverage is
outside default deterministic CI and runs through `make ci-opt-in-opentimestamps`.

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
deterministic CI and is covered by `make ci-opt-in-opentimestamps`.

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
- default: `18`

Scrypt work factor supplied during encryption through
`AGE_PASSPHRASE_WORK_FACTOR`.

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
- default: `96%`

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

## `RIVERHOG_GLACIER_UPLOAD_RETRY_LIMIT`

- type: integer
- default: `3`

Maximum number of automatic Glacier upload attempts per collection archive
package before the upload becomes a persistent failure.

## `RIVERHOG_GLACIER_UPLOAD_RETRY_DELAY`

- type: duration
- default: `5m`

Delay between automatic retry attempts for one failed Glacier upload.

## `RIVERHOG_GLACIER_UPLOAD_SWEEP_INTERVAL`

- type: duration
- default: `30s`

How often Riverhog's Glacier-upload worker scans for due collection archive
uploads, retries, and restart-recovered work.

Restart-recovered work resumes one durable job record. It does not resume one
interrupted multipart byte stream inside the remote object store.

## `RIVERHOG_GLACIER_FAILURE_WEBHOOK_URL`

- type: URL
- default: unset

Optional webhook endpoint notified when one collection Glacier archive upload
reaches persistent failure after automatic retries.

The payload includes the `collection_id`, archive package object paths, failure
timestamp, attempt count, and error context.

## `RIVERHOG_GLACIER_RECOVERY_SWEEP_INTERVAL`

- type: duration
- default: `30s`

How often Riverhog scans for due Glacier recovery-session transitions such as
restore-ready and expiry cleanup.

## `RIVERHOG_GLACIER_RECOVERY_RESTORE_LATENCY`

- type: duration
- default: `48h`

Operator-facing restore-latency estimate shown while one approved recovery
session waits for archive restore completion. Real readiness is driven by the
archive object's restore/readability status when a production archive store is
configured.

## `RIVERHOG_GLACIER_RECOVERY_READY_TTL`

- type: duration
- default: `24h`

How long Riverhog keeps restored Standard-storage collection archive data or
rebuilt ISO staging data available after the archive becomes ready before
automatic cleanup expires that recovery session.

When `RIVERHOG_GLACIER_RECOVERY_WEBHOOK_URL` is configured, this value must be at
least Riverhog's fixed 10-second outbound recovery-webhook timeout plus
`RIVERHOG_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY` so one failed ready notification can
still be retried before cleanup.

## `RIVERHOG_GLACIER_RECOVERY_WEBHOOK_URL`

- type: URL
- default: unset

Optional webhook endpoint notified when restored collection archive data or image
rebuild staging data becomes ready and when reminders are sent before cleanup
expiry.

## `RIVERHOG_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY`

- type: duration
- default: `60s`

Delay before Riverhog retries a failed recovery-ready webhook delivery.

With `RIVERHOG_GLACIER_RECOVERY_WEBHOOK_URL` configured, Riverhog rejects startup if
`RIVERHOG_GLACIER_RECOVERY_READY_TTL` is shorter than the fixed 10-second outbound
recovery-webhook timeout plus this retry delay.

## `RIVERHOG_GLACIER_RECOVERY_WEBHOOK_REMINDER_INTERVAL`

- type: duration
- default: `1h`

Interval between repeated ready reminders while restored collection archive data
or image rebuild staging data remains available and the recovery session is
still incomplete.

## `RIVERHOG_GLACIER_RECOVERY_RETRIEVAL_TIER`

- type: enum
- default: `bulk`

Retrieval tier used for recovery-session cost estimates.

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

## `RIVERHOG_GLACIER_BULK_RETRIEVAL_RATE_USD_PER_GIB`

- type: number
- default: `0.0025`

Manual per-GiB rate used when Riverhog estimates bulk Glacier retrieval cost
for one recovery session.

## `RIVERHOG_GLACIER_BULK_REQUEST_RATE_USD_PER_1000`

- type: number
- default: `0.025`

Manual request-fee rate used when Riverhog estimates bulk Glacier restore
request charges for one recovery session.

## `RIVERHOG_GLACIER_STANDARD_RETRIEVAL_RATE_USD_PER_GIB`

- type: number
- default: `0.02`

Manual per-GiB rate used when Riverhog estimates standard Glacier retrieval
cost for one recovery session.

## `RIVERHOG_GLACIER_STANDARD_REQUEST_RATE_USD_PER_1000`

- type: number
- default: `0.10`

Manual request-fee rate used when Riverhog estimates standard Glacier restore
request charges for one recovery session.

## `RIVERHOG_GLACIER_PRICING_LABEL`

- type: string
- default: `aws-s3-us-west-2-public`

Operator-facing label emitted when Glacier reporting stays on manual pricing or
falls back from AWS lookup.

## `RIVERHOG_GLACIER_PRICING_MODE`

- type: string
- default: `auto`

Controls how Riverhog resolves the Glacier storage-rate fields:

- `auto` tries AWS price-list lookup when the Glacier backend points at AWS S3,
  then falls back to the configured manual values
- `aws` requires AWS price-list lookup and fails if Riverhog cannot resolve the
  expected S3 pricing terms
- `manual` skips AWS lookup and always uses the configured values below

## `RIVERHOG_GLACIER_PRICING_API_REGION`

- type: string
- default: `us-east-1`

AWS Region for the Price List Bulk API endpoint. This is the pricing-API Region,
not the S3 product Region being priced.

## `RIVERHOG_GLACIER_PRICING_REGION_CODE`

- type: string
- default: `RIVERHOG_GLACIER_REGION`

AWS product RegionCode that Riverhog requests when it resolves S3 pricing from
AWS.

## `RIVERHOG_GLACIER_PRICING_CURRENCY_CODE`

- type: string
- default: `USD`

CurrencyCode that Riverhog requests when it resolves S3 pricing from AWS.

## `RIVERHOG_GLACIER_PRICING_CACHE_TTL`

- type: duration
- default: `24h`

How long one process keeps resolved AWS Glacier pricing before refreshing it.

## `RIVERHOG_GLACIER_BILLING_MODE`

- type: string
- default: `auto`

Controls whether Riverhog tries to resolve AWS Cost Explorer actuals and
forecast for Glacier reporting:

- `auto` tries AWS billing queries when the Glacier backend points at AWS S3
- `aws` requires AWS billing queries and fails if Cost Explorer data cannot be
  resolved
- `disabled` skips AWS billing queries and emits an unavailable billing summary

## `RIVERHOG_GLACIER_BILLING_API_REGION`

- type: string
- default: `us-east-1`

AWS Region for Cost Explorer API calls.

## `RIVERHOG_GLACIER_BILLING_CURRENCY_CODE`

- type: string
- default: `USD`

Currency that Riverhog expects from AWS billing responses.

## `RIVERHOG_GLACIER_BILLING_LOOKBACK_MONTHS`

- type: integer
- default: `3`

How many monthly Cost Explorer actual periods Riverhog requests for Glacier
reporting.

## `RIVERHOG_GLACIER_BILLING_FORECAST_MONTHS`

- type: integer
- default: `1`

How many future monthly Cost Explorer forecast periods Riverhog requests.

## `RIVERHOG_GLACIER_BILLING_VIEW_ARN`

- type: string
- default: unset

Optional AWS billing view ARN that Riverhog passes to
`GetCostAndUsageWithResources` when resolving bucket-scoped Glacier actuals.
When unset, Riverhog tries to discover the primary billing view automatically.

## `RIVERHOG_GLACIER_BILLING_EXPORT_BUCKET`

- type: string
- default: unset

Optional S3 bucket that stores CUR or Data Exports files for Glacier billing
drill-down.

## `RIVERHOG_GLACIER_BILLING_EXPORT_ARN`

- type: string
- default: unset

Optional AWS Data Exports ARN. When set, Riverhog selects the latest
successful export execution, resolves its manifest, and aggregates every file
referenced by that manifest.

## `RIVERHOG_GLACIER_BILLING_EXPORT_PREFIX`

- type: string
- default: unset

Optional S3 prefix inside `RIVERHOG_GLACIER_BILLING_EXPORT_BUCKET` that Riverhog
scans for the most recent CUR or Data Exports manifest when no explicit export
ARN is configured.

## `RIVERHOG_GLACIER_BILLING_EXPORT_REGION`

- type: string
- default: `us-east-1`

AWS Region for the S3 bucket that stores CUR or Data Exports billing detail.

## `RIVERHOG_GLACIER_BILLING_EXPORT_MAX_ITEMS`

- type: integer
- default: `10`

Maximum number of aggregated CUR or Data Exports breakdown rows Riverhog emits
in Glacier billing output.

## `RIVERHOG_GLACIER_BILLING_TAG_KEY`

- type: string
- default: unset

Optional cost-allocation tag key for Glacier billing scope. When paired with
`RIVERHOG_GLACIER_BILLING_TAG_VALUE`, Riverhog uses tag-scoped Cost Explorer
forecast and fallback actuals instead of the broader Amazon S3 service scope.
The same tag filter is also used for CUR or Data Exports drill-down when
configured.

## `RIVERHOG_GLACIER_BILLING_TAG_VALUE`

- type: string
- default: unset

Optional cost-allocation tag value for Glacier billing scope.

## `RIVERHOG_GLACIER_BILLING_INVOICE_ACCOUNT_ID`

- type: string
- default: unset

Optional AWS account ID used for invoice-summary lookup. When unset, Riverhog
tries to resolve the caller account through STS.

## `RIVERHOG_GLACIER_BILLING_INVOICE_MAX_ITEMS`

- type: integer
- default: `6`

Maximum number of AWS invoice summaries Riverhog requests for Glacier billing
output.

## `RIVERHOG_GLACIER_STORAGE_RATE_USD_PER_GIB_MONTH`

- type: number
- default: `0.00099`

Manual override and fallback for the Glacier storage rate used when Riverhog
estimates recurring monthly archive cost from measured uploaded bytes.

## `RIVERHOG_GLACIER_STANDARD_RATE_USD_PER_GIB_MONTH`

- type: number
- default: `0.023`

Manual override and fallback for the S3 Standard storage rate used for the
8 KiB per-object metadata overhead component in Glacier usage estimates.

## `RIVERHOG_GLACIER_ARCHIVED_METADATA_BYTES_PER_OBJECT`

- type: integer
- default: `32768`

Configured Glacier-billed metadata overhead bytes added per archived object when
Riverhog estimates billable storage.

## `RIVERHOG_GLACIER_STANDARD_METADATA_BYTES_PER_OBJECT`

- type: integer
- default: `8192`

Configured S3 Standard-billed metadata overhead bytes added per archived object
when Riverhog estimates billable storage.

## `RIVERHOG_GLACIER_MINIMUM_STORAGE_DURATION_DAYS`

- type: integer
- default: `180`

Configured minimum storage-duration assumption published with Glacier usage
reporting. Riverhog emits this as part of the pricing basis but does not fold
it into recurring monthly storage totals. Riverhog keeps this constant explicit
instead of resolving it from the price-list API.

## `RIVERHOG_TUSD_BASE_URL`

- type: URL

Base URL for the internal `tusd` service that owns resumable staging uploads.
Riverhog remains the public upload contract and maps logical upload resources to
internal `tusd` uploads.

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

- type: SQLAlchemy database URL
- default: `postgresql+psycopg://riverhog:riverhog@127.0.0.1:5432/riverhog`

This is the catalog database URL used for durable authoritative API state. The
checked-in Compose stack sets this to the Postgres sidecar at `postgres:5432`.

## `RIVERHOG_PUBLIC_BASE_URL`

- type: URL
- default: unset

Optional public API base URL used when Riverhog builds webhook links back to
collection uploads, collection restore sessions, image rebuild sessions, and
finalized-image ISO downloads.

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

## `UPLOAD_EXPIRY_SWEEP_INTERVAL`

- type: duration
- default: `30s`

This controls how often Riverhog's background expiry reaper sweeps collection-upload and fetch-upload state looking
for entries whose published `INCOMPLETE_UPLOAD_TTL` has already elapsed.

Lower values reduce how long expired upload state may remain present after its TTL boundary. Higher values reduce
background sweep frequency at the cost of slower cleanup after expiry.
