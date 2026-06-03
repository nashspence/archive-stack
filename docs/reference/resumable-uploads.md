# Resumable Uploads Reference

Riverhog uses the same resumable-upload lifecycle for collection ingest and fetch recovery:

- the JSON API binds uploads to a server-owned domain resource
- the returned upload resource uses tus-compatible resumable upload semantics within the contract published for that workflow
- incomplete bytes stage under `.riverhog/uploads/` rather than appearing immediately as committed hot files
- Riverhog promotes staged collection bytes to `collections/{collection_id}/{path}`
  only after file verification and collection Glacier archive verification
- upload state survives service restart until `INCOMPLETE_UPLOAD_TTL` expires
- expiry cancels the upload resource, deletes incomplete server-side bytes, and resets the domain resource cleanly

For collection ingest specifically:

- the terminal successful collection-file upload chunk may move the collection
  upload from `uploading` to `archiving` once every required file verifies
- the collection upload reaches `finalized` only after the whole-collection
  Glacier archive package uploads and verifies
- archival finalization failures leave the collection upload `archiving` with a
  retry phase, keep retrying indefinitely, notify the operator on a paced
  cadence, and keep the collection invisible until retry succeeds
- `riverhog upload` waits for staged handoff by default, which means all files
  have reached Riverhog and the server has accepted responsibility for archive
  finalization; operators can use `--wait finalized` when a stricter blocking
  client run is desired
- staged collection bytes are retained until the finalized collection and
  archive records commit; post-finalization staging cleanup is best-effort, so a
  restart cannot make a retry depend on already-deleted staged bytes
- the S3 multipart upload used for the collection Glacier object is also
  restart-resumable while the remote multipart upload still exists
- once the last resumable collection-file state expires, Riverhog forgets the upload session instead of keeping an empty pending record

## Collection Upload Creation

`POST /v1/collection-uploads` creates or resumes a collection upload from a
human-readable slug and a complete file manifest. Clients do not provide
collection ids.

Riverhog normalizes the slug, mints the canonical collection id with the server
UTC upload timestamp, and returns that id for all later file-upload and status
calls. Migration uploads may provide `upload_timestamp` in UTC basic form
`YYYYMMDDTHHMMSSZ` to preserve the original archival timestamp; the slug is
still required. Collection ids are shaped like:

```text
2026/20260524T190233Z__mom-iphone-photos
```

Retry behavior is manifest-aware:

- the same normalized slug and same file manifest resumes the existing
  non-finalized upload
- the same normalized slug and same file manifest returns the finalized
  collection payload after the collection has already completed
- the same normalized slug with a different file manifest creates a separate
  timestamped collection id

## Collection File Upload Session

`POST /v1/collection-uploads/{collection_id}/files/{path}/upload` creates or resumes the upload resource for one logical
collection file.

The returned `upload_url` is a Riverhog-managed tus-compatible upload resource for that logical file. Riverhog supports:

- `HEAD` to read `Upload-Offset`, `Upload-Length`, `Upload-Expires`, and `Location`
- `PATCH` to append bytes using `Content-Type: application/offset+octet-stream` plus `Tus-Resumable`, `Upload-Offset`, and `Upload-Checksum`
- `DELETE` to cancel the current upload resource and reset that file back to `pending`
- `OPTIONS` to advertise the supported tus capability headers

The response exposes at least:

- `path`
- `protocol` — always `tus`
- `upload_url`
- `offset`
- `length`
- `expires_at`
- `checksum_algorithm`

## Collection Archive Multipart Resume

Collection archive upload is a second resumable phase after all logical files
are staged. Riverhog creates one deterministic tar archive for the collection
and stores it with sibling manifest/proof objects at:

```text
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/archive.tar
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/manifest.yml
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/manifest.yml.ots
```

The tar contains only the logical files and uses the configured Glacier storage
class. The collection manifest and OTS proof are separate Standard S3 objects
under the same collection prefix.

When `RIVERHOG_GLACIER_ARCHIVE_ENCRYPTION=age_scrypt`, those object names gain a
`.age` suffix and the stored objects are standard binary age v1 scrypt files:

```text
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/archive.tar.age
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/manifest.yml.age
{RIVERHOG_GLACIER_PREFIX}/collections/{collection_id}/manifest.yml.ots.age
```

Riverhog still records and verifies the logical plaintext archive, manifest, and
proof byte counts and SHA-256 hashes. S3 object metadata also records the
encryption mode and plaintext size/hash so an encrypted object can be validated
without confusing stored ciphertext bytes with collection bytes.

For archives that use S3 multipart upload, Riverhog persists the multipart
`UploadId`, object key, part size, archive length, archive SHA-256, and progress
on the collection upload row. It also records each uploaded part number, ETag,
and size. If the app restarts or an upload attempt fails, the retry lists
uploaded parts for that `UploadId`, verifies them against the recorded part
metadata, skips the already uploaded contiguous prefix of the deterministic
archive stream, uploads the remaining parts, and completes the same multipart
upload using the recorded part numbers and ETags.

Encrypted archive multipart uploads additionally persist the age header and
payload nonce as resumable encryption state. On retry, Riverhog derives the file
key from the configured passphrase, regenerates the exact same age ciphertext
parts at age chunk boundaries, verifies the already accepted S3 parts, and then
continues the same multipart upload. The plaintext age file key is not stored in
the database.

Multipart archive parts are uploaded with bounded per-collection concurrency,
configured by `RIVERHOG_GLACIER_MULTIPART_CONCURRENCY`. This is a throughput
tuning knob only; resumability still comes from the persisted S3 multipart
upload id and recorded part metadata.

After Riverhog has stamped the manifest and measured the deterministic tar
stream, it persists the package manifest, proof, byte count, and archive SHA-256
on the upload row before starting S3 multipart upload. A restart after that
`packaged` point reuses the same package artifacts and proceeds directly to the
resumable upload instead of restamping or remeasuring the archive.

After S3 accepts the completed archive object, Riverhog persists the archive
receipt on the same upload row before promoting hot files. A restart after
Glacier completion therefore resumes from the recorded receipt instead of
rebuilding or re-uploading the archive.

After finalization, under-protected collections remain pinned in hot storage.
The Glacier recovery worker audits active pins at startup and before planner
refreshes. Missing pinned files with registered disc coverage wait for the
normal `djdan fetch` flow; missing pinned files without registered disc coverage
create or resume an automatic collection Glacier restore session. Once S3 makes
the archive package readable, Riverhog verifies the manifest, proof, and
selected archive members before writing those files back to hot storage.

During promotion, each hot file is written with byte and
SHA-256 metadata and marked promoted only after the hot object verifies. For
S3-compatible hot stores, in-progress per-file multipart uploads also persist
their upload id and uploaded parts on the upload-file row, so a process restart
can list the remote parts, skip the already accepted byte range, and complete
the same hot-store multipart upload. Retries skip verified hot files, finish the
remaining files, then atomically commit the finalized collection and archive
records before deleting staged upload objects.

When `RIVERHOG_OPERATOR_WEBHOOK_URL` is configured, Riverhog emits best-effort
milestone notifications across these phases. These notifications are
intentionally sparse: upload staged, collection finalized, ready disc-image
candidates, recovery readiness, and persistent failures. The collection
finalized notification is the reassuring handoff: the archive is safely in
Glacier, hot-file promotion is complete, and the collection is available through
Riverhog/WebDAV. The CLI can therefore exit at the staged handoff while
operators still receive phone or automation updates for the important handoffs
without per-part or per-retry noise.

This follows Amazon S3's multipart contract: the upload id is required to upload
parts, list parts, complete, or abort; completion requires part numbers and
ETags; and uploading the same part number replaces that part. S3 also bills
incomplete multipart parts until the upload is completed or aborted, so the
bucket lifecycle rule for aborting old incomplete multipart uploads remains a
backup guard. See the AWS S3 multipart overview:
https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html

## Planner Materialization Resume

After a collection has finalized and archive artifacts have cached locally,
Riverhog refreshes provisional optical-disc candidates. Candidate rows are
created before materialization starts with stable candidate ids derived from the
planned contents and planner sizing config. If the final candidate is below
`RIVERHOG_PLANNER_MIN_FILL_BYTES` or `RIVERHOG_PLANNER_MIN_FILL_RATIO`, it is
kept in `waiting` state and no image root is materialized; those files stay
safe in Glacier and wait for future collections to fill a burnable disc.
If waiting candidate bytes exceed `RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES`,
Riverhog may add fair beneficial whole-file voluntary collection splits,
including for collections that already required splitting, to create enough
filled candidates to bring waiting bytes back under the saturation threshold.
Underfilled candidates remain in `waiting` state.

A burnable candidate root is first written as
`.candidate-*.tmp`; completed encrypted payloads, sidecars, manifests, and
readme files are left in that temp root if materialization fails or the app
restarts.

Planner refreshes use a filesystem lock under `RIVERHOG_PLANNER_IMAGE_ROOT` so
only one app or operator-triggered process can delete stale candidates or write
candidate temp roots at a time.

The next planner refresh reuses the same candidate row and temp root, skips
already completed encrypted files, finishes the missing files, then atomically
renames the temp root to the final candidate root and marks the candidate
`ready`. A background planner refresh worker checks for missing, failed,
waiting, or still-materializing provisional candidates every
`RIVERHOG_PLANNER_REFRESH_SWEEP_INTERVAL`, with one refresh queued immediately
on API startup.

## Fetch Entry Upload Session

`POST /v1/fetches/{fetch_id}/entries/{entry_id}/upload` creates or resumes the upload resource for one recovery-manifest
entry.

Fetch manifest entries identify the logical file with both `collection_id` and
`path`. This keeps recovery unambiguous when one fetch spans multiple
collections that contain the same relative path.

The returned `upload_url` is a Riverhog-managed tus-compatible upload resource for that manifest entry. Riverhog
supports:

- `HEAD` to read `Upload-Offset`, `Upload-Length`, `Upload-Expires`, and `Location`
- `PATCH` to append bytes using `Content-Type: application/offset+octet-stream` plus `Tus-Resumable`, `Upload-Offset`, and `Upload-Checksum`
- `DELETE` to cancel the current upload resource and reset that manifest entry back to `pending`
- `OPTIONS` to advertise the supported tus capability headers

The response exposes at least:

- `entry`
- `protocol` — always `tus`
- `upload_url`
- `offset`
- `length`
- `expires_at`
- `checksum_algorithm`

## Shared Transport Semantics

Every upload resource must support:

- offset-based resume
- expiration
- checksum validation on streamed chunks
- restart-safe resume until the published expiry time discards incomplete state
- stale background sync or expiry work must not roll committed upload progress backward after a
  request-driven transition has already verified, consumed, or reset that upload resource

Collection uploads measure offsets against the logical file byte stream for that file.
Collection upload resources expose Riverhog-managed tus-compatible `HEAD`/`PATCH`/`DELETE`/`OPTIONS` semantics on the
published `upload_url`.

Fetch uploads measure offsets against the ordered recovery-byte stream for that manifest entry. Fetch upload resources
expose Riverhog-managed tus-compatible `HEAD`/`PATCH`/`DELETE`/`OPTIONS` semantics on the published `upload_url`.
Once the recovery-byte stream reaches full length, the manifest entry becomes `byte_complete`; it does not become
`uploaded` until `POST /v1/fetches/{fetch_id}/complete` verifies and materializes the recovered logical file.
Split files still use one upload resource per logical file; `djdan` streams parts into that one resource in
ascending order.
The server records exact encrypted payload length for every registered disc copy and lazily backfills missing encrypted
payload SHA-256 metadata before returning a fetch manifest, so cold-only fetches can publish their manifest, resume
uploads, and complete verification without using hot plaintext as an input.

CLI uploads use bounded request chunks plus paced socket writes. The default
request chunk size is 8 MiB. The default write pacing is 256 KiB sub-writes with
a 0.005 second delay, which keeps upload progress stable on paths where
aggressive client-side bulk writes can stall below HTTP. Operators may tune
`RIVERHOG_UPLOAD_CHUNK_BYTES`, `RIVERHOG_UPLOAD_WRITE_CHUNK_BYTES`, and
`RIVERHOG_UPLOAD_WRITE_DELAY_SECONDS` after validating the target network and
reverse-proxy body limits. After all files are staged, `riverhog upload` exits
by default; `--wait finalized` polls until the collection finalizes, and
`RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS` can bound that wait when automation
needs a hard deadline. See
[Upload Transport Reference](upload-transport.md) for the operational findings
and tuning guidance.

When `complete` rejects `byte_complete` recovery bytes, the canonical operator recovery path is an explicit
`DELETE` of the affected fetch-entry upload resource before retry. `djdan fetch` performs that reset for entries it
has made byte-complete, reports that the fetch remains active and incomplete, and lets the next attempt start from offset
`0` with another registered copy or with media restored through the Glacier recovery workflow.
