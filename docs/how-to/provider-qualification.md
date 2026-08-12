# Provider qualification

The provider qualification is an operator-deployable, resumable integration test for the
v1 release. The Python runner is the authority; the GitHub Actions workflow is one adapter.
The GitHub adapter never connects to an existing Riverhog database or accepts an
operator-selected corpus. Other adapters must provide the same disposable boundary.

Each run creates a deterministic synthetic collection, uploads it through the official
Riverhog client, and exercises these provider roles:

- private B2 immediate archive, written directly by collection upload;
- private B2 retrieval cache;
- private AWS S3 Deep Archive; and
- private, signed-URL CloudFront egress from the restored Deep Archive objects.

CloudFront is required. A successful run compares every CloudFront ciphertext byte and
SHA-256 identity with direct S3 recovery and requires the second signed HTTPS request to be
a cache hit.

## Lifecycle

`make provider-qualification args="infrastructure apply <config>"` creates or reconciles
only the dedicated AWS S3 and CloudFront resources bearing the exact
`riverhog-provider-qualification` ownership marker. It refuses an existing unmarked bucket.
B2 provisioning is intentionally manual because the archive and retrieval-cache application
keys must be created after their dedicated buckets and scoped to those exact buckets.
`make provider-qualification args="b2-check <config>"` is the read-only conformance check.
Runs use a UUID-qualified object prefix, and the AWS bucket must never have had versioning
enabled. The runner retires its B2 archive copies and retains the small Deep Archive canary
until its 185-day lifecycle expiration to avoid premature-deletion charges.

One `operate` invocation advances all immediately available work and returns while a Deep
Archive restore is pending. The next invocation reconstructs a disposable Riverhog service
from its checkpoint and database snapshot and continues. `checkpoint-show` verifies the
checkpoint digest, exact source, corpus identity, provider binding, and phase history.
Terminal `evidence` includes only logical providers, regions/classes, byte counts, hashes,
phase timings, and pass/fail status.

The GitHub adapter runs every six hours while work is active. It starts regular and
multipart qualifications on approximately alternating half-month boundaries. Its database
is always a fresh local Compose PostgreSQL service populated exclusively by the deterministic
corpus. While a restore remains active, a bounded PostgreSQL custom-format dump and its
checkpoint are retained as a 14-day Actions artifact. The dump contains dummy qualification
state, never credentials or live data. Terminal artifacts omit the dump.

The multipart profile terminates the official upload client after observing a committed
upload unit, then restarts it with the same idempotency key and requires finalization.

## Configuration

Start with [`config/provider-qualification.example.toml`](../../config/provider-qualification.example.toml).
The same commands work from a workstation, another scheduler, or GitHub Actions. Run
`make provider-qualification args="--help"` for the current command surface.

The operator supplies these non-secret resource values:

- `RIVERHOG_QUALIFICATION_AWS_DEEP_ARCHIVE_BUCKET`
- `RIVERHOG_QUALIFICATION_AWS_REGION`
- `RIVERHOG_QUALIFICATION_B2_ARCHIVE_BUCKET`
- `RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_BUCKET`
- `RIVERHOG_QUALIFICATION_B2_REGION`
- `RIVERHOG_QUALIFICATION_B2_S3_ENDPOINT_URL`

The runtime also requires an archive passphrase, a disposable Riverhog bootstrap token,
matching RSA-2048 CloudFront signing keys, and separately scoped credentials for each B2
bucket. GitHub Actions obtains AWS credentials through two OIDC roles:

- a provisioning role, used only when starting a run, that can inspect/create/configure the
  dedicated S3 bucket, CloudFront public key, trusted key group, origin access control,
  distribution, and restricted origin bucket policy; and
- a runtime role limited to the qualification bucket and prefix with the S3 multipart,
  object, listing, restore, and metadata operations needed by Riverhog, plus read access to
  the marked CloudFront configuration.

Create both B2 buckets manually as private, S3-compatible, dedicated buckets with Object
Lock disabled, no CORS rules, default SSE-B2 encryption, and this exact lifecycle for the
`qualification/` prefix: hide current objects after seven days, delete hidden versions after
one day, and cancel unfinished large files after three days. Object Lock is the one manual
assertion because ordinary bucket-scoped runtime keys cannot inspect retention settings.
For each bucket, create a distinct application key restricted to that exact bucket. A key
may additionally be restricted to `qualification/`. It needs the B2 capabilities that back
S3 list/read/write/delete operations plus `listBuckets`, `readBucketEncryption`, and
`readBucketLifecycleRules`. The read-only `b2-check` verifies the key scope, endpoint,
region, privacy, CORS, encryption, and lifecycle before any disposable deployment starts.

GitHub uses two protected environments. Both permit deployments from the exact `main`
branch only and prohibit administrator bypass. `provider-qualification-provisioning`
requires explicit maintainer approval and exposes only AWS infrastructure-reconciliation
authority. It has these environment variables:

- `RIVERHOG_QUALIFICATION_AWS_DEEP_ARCHIVE_BUCKET`;
- `RIVERHOG_QUALIFICATION_AWS_REGION`;
- `RIVERHOG_QUALIFICATION_AWS_PROVISION_ROLE_ARN`;
- `RIVERHOG_QUALIFICATION_CLOUDFRONT_PUBLIC_KEY`.

It has no environment secrets. AWS credentials are short-lived and obtained through OIDC.

The `provider-qualification` runtime environment has no reviewer gate so six-hour polling
can continue unattended. It has all six resource variables above plus:

- `RIVERHOG_QUALIFICATION_AWS_RUNTIME_ROLE_ARN`;
- `RIVERHOG_QUALIFICATION_B2_ARCHIVE_ACCESS_KEY_ID`;
- `RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_ACCESS_KEY_ID`; and
- `RIVERHOG_QUALIFICATION_CLOUDFRONT_PUBLIC_KEY`.

Its environment secrets are:

- `RIVERHOG_QUALIFICATION_B2_ARCHIVE_SECRET_ACCESS_KEY`;
- `RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_SECRET_ACCESS_KEY`;
- `RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE`;
- `RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN`; and
- `RIVERHOG_QUALIFICATION_CLOUDFRONT_PRIVATE_KEY`.

A fresh run pauses once for AWS provisioning approval, then enters the runtime environment
and verifies the manually provisioned B2 boundary before starting the disposable deployment.
Continuation runs skip provisioning and enter only the unattended runtime environment.
Each AWS OIDC role trusts only its corresponding environment subject; the exact-`main`
environment branch policy and workflow authority check supply the branch boundary. The
workflow uses SHA-pinned actions and grants `id-token: write` only to the two jobs that
obtain AWS credentials.
