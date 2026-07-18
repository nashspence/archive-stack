# Archive operations

Riverhog's configured archive stores are its durable storage authority. Operational
readiness therefore includes human and provider controls that application tests cannot
establish.

## Account readiness

Keep account recovery, multi-factor authentication, payment, billing alerts, credentials,
bucket permissions, provider contacts, and archive-passphrase custody current. Periodically
exercise object listing, metadata reads, retrieval requests, and downloads in every store.

After account, credential, provider, or storage-class changes, run an authenticated archive
report and retrieve known files through the external application interface from the affected
store. A report or object listing alone does not establish recoverability.

An AWS archive store may route encrypted object downloads through a private CloudFront
distribution while retaining S3 as the authority for writes, metadata, restore state, and
deletion. Set that store's `CLOUDFRONT_BASE_URL`, `CLOUDFRONT_PUBLIC_KEY_ID`, and
`CLOUDFRONT_PRIVATE_KEY_PATH` settings together to enable it; leave all three unset for
direct S3 downloads. Partial configuration is invalid, and a configured CloudFront failure
does not silently fall back to S3.

An archive store may enforce a UTC-calendar-month download allowance. Riverhog reserves
each encrypted object before opening the remote read and accounts for the ciphertext bytes
it receives, including partial reads and retries. Set that store's
`MONTHLY_DOWNLOAD_ALLOWANCE_BYTES` and optional `DOWNLOAD_SAFETY_BUFFER_BYTES`; the
buffer must be smaller than the allowance, and a nonzero buffer without an allowance is
invalid. The archive report shows current usage, reservations, remaining bytes, and reset
time.

Riverhog maintains plaintext `README.md` and `AGENTS.md` guidance at each archive root.
Opaque names do not mean objects are unused; encrypted collection objects may be the sole
durable copies.

## Archive copies

`riverhog archive copy --help` copies a collection between configured archive stores. The
background job verifies the source object set, prepares provider-managed archive objects for
reading when necessary, writes an independently encrypted destination object set, and
records it only after destination verification.

`riverhog archive retire --help` removes one exact collection-and-store copy. Inspect the
plan and confirm the exact collection, selected store, retained verification candidates,
affected objects and bytes, blockers, warning, and short-lived challenge. Execution requires
a different complete copy to pass current remote verification before removing the selected
object set and refreshing the affected encrypted catalogs.

## Collection deletion

`riverhog collection delete --help` is the deletion interface. Inspect its plan and confirm
the exact collection id, sole-copy warning, affected objects and bytes, active-work blockers,
and short-lived challenge before execution.

Successful deletion removes archive objects in every store, leased retrieval-cache objects,
recovery-catalog entries, and catalog projections. Provider retention, object versions,
minimum-storage duration, or billing timing may delay visible cost changes. External
applications discover deletion through ResourceSync and decide how to handle their own
local copies.

Direct provider credentials can bypass Riverhog's ceremony. Protect those credentials and
treat provider-console or raw-object deletion as an exceptional custody operation.
