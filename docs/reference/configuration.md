# Configuration reference

Riverhog reads runtime settings from environment variables. `.env.compose.example` is the canonical fake local-stack example.

## Catalog and hot storage

- `RIVERHOG_DATABASE_URL`: PostgreSQL SQLAlchemy URL.
- `RIVERHOG_OBJECT_STORE`: `s3`.
- `RIVERHOG_S3_ENDPOINT_URL`, `RIVERHOG_S3_REGION`, `RIVERHOG_S3_BUCKET`: hot object-store location.
- `RIVERHOG_S3_ACCESS_KEY_ID`, `RIVERHOG_S3_SECRET_ACCESS_KEY`: hot-storage credentials.
- `RIVERHOG_S3_FORCE_PATH_STYLE`: S3 addressing mode.
- `RIVERHOG_S3_MAX_POOL_CONNECTIONS`: client connection-pool limit.
- `RIVERHOG_HOT_PROMOTION_CONCURRENCY`: parallel file materializations.
- `RIVERHOG_HOT_SINGLE_PUT_MAX_BYTES`: threshold for single-request hot writes.

## Remote archive

- `RIVERHOG_ARCHIVE_ENDPOINT_URL`, `RIVERHOG_ARCHIVE_REGION`, `RIVERHOG_ARCHIVE_BUCKET`: archive location.
- `RIVERHOG_ARCHIVE_ACCESS_KEY_ID`, `RIVERHOG_ARCHIVE_SECRET_ACCESS_KEY`: archive credentials.
- `RIVERHOG_ARCHIVE_FORCE_PATH_STYLE`: archive S3 addressing mode.
- `RIVERHOG_ARCHIVE_PREFIX`: opaque object-key namespace.
- `RIVERHOG_ARCHIVE_STORAGE_CLASS`: provider storage class.
- `RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES`, `RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY`: multipart upload limits.
- `RIVERHOG_ARCHIVE_ENCRYPTION`: `age_scrypt`.
- `RIVERHOG_ARCHIVE_PASSPHRASE`: archive encryption secret.
- `RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE`: require a deployment-supplied secret.
- `RIVERHOG_ARCHIVE_WORK_FACTOR`: scrypt work factor.
- `RIVERHOG_ARCHIVE_UPLOAD_RETRY_DELAY`, `RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL`: upload worker timing.
- `RIVERHOG_OTS_STAMP_COMMAND`, `RIVERHOG_OTS_VERIFY_COMMAND`: OpenTimestamps commands.

## Archive retrieval

- `RIVERHOG_ARCHIVE_RESTORE_MODE`: `auto` or `aws`.
- `RIVERHOG_ARCHIVE_RESTORE_RETRIEVAL_TIER`: `bulk` or `standard`.
- `RIVERHOG_ARCHIVE_RESTORE_LATENCY`: expected provider latency.
- `RIVERHOG_ARCHIVE_RESTORE_READY_TTL`: provider-ready window.
- `RIVERHOG_ARCHIVE_RESTORE_SWEEP_INTERVAL`: worker cadence.

Durations accept combined day, hour, minute, and second units such as `2d`, `6h`, and `30s`. Byte settings accept SI or IEC units such as `64MiB`.

## Upload staging

- `RIVERHOG_TUSD_BASE_URL`: internal tusd endpoint.
- `RIVERHOG_TUSD_PUBLIC_BASE_URL`: optional client-facing tusd endpoint.
- `RIVERHOG_TUSD_HOOK_SECRET`: hook authentication secret.
- `RIVERHOG_TUSD_PUBLIC_SIGNING_SECRET`: optional signed public-upload URL secret.
- `RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS`: append request timeout.
- `RIVERHOG_UPLOAD_STAGING_ROOT`: staged upload filesystem root.
- `INCOMPLETE_UPLOAD_TTL`: determinate upload expiry.
- `RIVERHOG_UPLOAD_SESSION_IDLE_TTL`: incremental session expiry.
- `UPLOAD_EXPIRY_SWEEP_INTERVAL`: expiry worker cadence.

The CLI uses `RIVERHOG_UPLOAD_CHUNK_BYTES`, `RIVERHOG_UPLOAD_FILE_CONCURRENCY`, `RIVERHOG_UPLOAD_FILE_LOG_BYTES`, `RIVERHOG_UPLOAD_TIMEOUT_SECONDS`, `RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS`, and `RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS`.

## Notifications and service

- `RIVERHOG_OPERATOR_WEBHOOK_URL`: canonical operator webhook.
- `RIVERHOG_NOTIFY_WEBHOOKS`: recipient-to-URL JSON map.
- `RIVERHOG_NOTIFY_DEFAULT_RECIPIENTS`: comma-separated default recipients.
- `RIVERHOG_OPERATOR_WEBHOOK_TIMEOUT`, `RIVERHOG_OPERATOR_WEBHOOK_RETRY_DELAY`: delivery timing.
- `RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL`, `RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME`, `RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIMEZONE`: reminder schedule.
- `RIVERHOG_PUBLIC_BASE_URL`: public API base URL used in notifications.
- `RIVERHOG_LOG_LEVEL`: service log level.
- `RIVERHOG_WEBDAV_ENABLED`, `RIVERHOG_WEBDAV_ADDR`: read-only browsing service.

Secrets belong in deployment secret management, never in checked public configuration.
