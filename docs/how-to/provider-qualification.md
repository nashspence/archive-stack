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

`make provider-qualification args="infrastructure apply <config>"` creates missing
resources or reconciles resources bearing the exact `riverhog-provider-qualification`
ownership marker. It refuses an existing unmarked bucket. Runs use a UUID-qualified object
prefix, and the AWS bucket must never have had versioning enabled. The runner retires its B2
archive copies and retains the small Deep Archive canary until its 185-day lifecycle
expiration to avoid premature-deletion charges.

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

The B2 provisioning key needs `listBuckets`, `writeBuckets`, `readBucketEncryption`,
`writeBucketEncryption`, and `readBucketRetentions`. The final capability lets the
reconciler positively prove that Object Lock is disabled. Each per-bucket application key
is restricted to its one dedicated bucket and the `qualification/` prefix, with the
list/read/write/delete capabilities needed for that role. The provisioning key is
unavailable during polling invocations.

The protected GitHub environment is named `provider-qualification`. Its secret names are
the environment-variable names above plus:

- `RIVERHOG_QUALIFICATION_AWS_PROVISION_ROLE_ARN`
- `RIVERHOG_QUALIFICATION_AWS_RUNTIME_ROLE_ARN`
- `RIVERHOG_QUALIFICATION_B2_PROVISION_KEY_ID`
- `RIVERHOG_QUALIFICATION_B2_PROVISION_APPLICATION_KEY`
- `RIVERHOG_QUALIFICATION_B2_ARCHIVE_ACCESS_KEY_ID`
- `RIVERHOG_QUALIFICATION_B2_ARCHIVE_SECRET_ACCESS_KEY`
- `RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_ACCESS_KEY_ID`
- `RIVERHOG_QUALIFICATION_B2_RETRIEVAL_CACHE_SECRET_ACCESS_KEY`
- `RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE`
- `RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN`
- `RIVERHOG_QUALIFICATION_CLOUDFRONT_PUBLIC_KEY`
- `RIVERHOG_QUALIFICATION_CLOUDFRONT_PRIVATE_KEY`

AWS OIDC trust is restricted to the repository, the default-branch workflow authority, and
the `provider-qualification` environment. The workflow uses SHA-pinned actions and has only
`actions: read`, `contents: read`, and `id-token: write` permissions.
