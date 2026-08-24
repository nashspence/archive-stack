# Provider qualification

This is the operator-runnable, resumable provider proof for the v1 release. The Python
runner is authoritative; GitHub Actions is one scheduler adapter. Every run uses a fresh
database and deterministic synthetic data. It must never connect to a live Riverhog database
or use an operator corpus.

Run `make provider-qualification args="--help"` for the command surface. The checked input is
[`config.toml`](config.toml). A typical run starts with `infrastructure apply`, verifies the
manual B2 boundary with `b2-check`, and advances with repeated `operate` invocations while AWS
Deep Archive restore is pending.

## Provider setup

AWS provisioning is automated and limited to dedicated resources bearing the exact
`riverhog-provider-qualification` ownership marker. It creates or reconciles the S3 Deep
Archive and CloudFront resources; it refuses unmarked existing buckets. Use separate OIDC
roles for provisioning and runtime as declared by
[`provider-qualification.yml`](../../.github/workflows/provider-qualification.yml).

Create the B2 archive and retrieval-cache buckets and their application keys manually:

- use dedicated private S3-compatible buckets;
- disable Object Lock and CORS;
- set prior versions to one day;
- create one key per bucket, scoped to that exact bucket and optionally `qualification/`;
- permit the S3 list, read, write, delete, and multipart-cleanup operations plus the B2
  bucket-list and lifecycle-read capabilities required by `b2-check`.

Riverhog objects are already authenticated ciphertext, so B2 server-side encryption is not a
qualification requirement. `b2-check` validates scope, endpoint, region, privacy, CORS, and
lifecycle without mutating either bucket.

## GitHub boundary

The `provider-qualification-provisioning` environment requires maintainer approval. The
unattended `provider-qualification` environment permits continuation polling. Both accept only
the exact `main` branch and prohibit administrator bypass. Populate the variables and secrets
referenced by the workflow; never use live deployment credentials.

CloudFront qualification intentionally uses standard pay-as-you-go billing so eligible
account-wide transfer is charged against that plan's allowance. It does not enroll in a
flat-rate plan. The reconciler disables optional paid features, uses `PriceClass_100`, and the
runner limits qualification downloads to 2 GiB per month. Other account traffic shares any
allowance, so the operator remains responsible for billing review.

The AWS Deep Archive canary is retained for its minimum-charge lifetime. Terminal cleanup
removes disposable B2 versions, delete markers, and multipart uploads from the run prefix.
