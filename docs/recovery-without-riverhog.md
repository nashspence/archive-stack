# Recovery without Riverhog

An archive copy can be recovered without a Riverhog server or database. Recovery requires
only the encrypted objects from one complete archive directory, the owner's archive
passphrase, and standard age-compatible tooling or the Apache-licensed reference utility.

Treat the archive source as read-only. Work from downloaded copies and never move, rename,
overwrite, expire, or delete provider objects while recovering them.

## Locate a collection

Archive directories have opaque identifiers beneath `archives/`. Decrypt each small mutable
metadata record until the desired collection is found:

```sh
aws s3 ls s3://BUCKET/PREFIX/archives/
aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID/metadata.yml.age .
age --decrypt -o metadata.yml metadata.yml.age
```

`PREFIX/` is optional. `metadata.yml` records the integer collection id, current tags, and
content identity. It is mutable discovery metadata and is deliberately outside the signed
immutable object inventory.

## Obtain and verify an archive copy

Download the selected directory, including `manifest.yml.age`, `manifest.yml.ots.age`, its
`objects/` directory, and any `SHA256SUMS*` files. Objects in a provider archive class must
complete provider-side restoration before they can be downloaded.

```sh
aws s3 cp s3://BUCKET/PREFIX/archives/ARCHIVE_ID ./archives/ARCHIVE_ID \
  --recursive \
  --force-glacier-transfer
```

The AWS CLI requires `--force-glacier-transfer` for restored Glacier-class objects even
after their restore status reports ready. The flag does not initiate restoration; use it
only after every needed object has completed its provider-side restore.

When an attestation is present, obtain the Minisign public key from an independently trusted
location and verify the inventory before recovery:

```sh
minisign -Vm SHA256SUMS -x SHA256SUMS.minisig -p /path/to/trusted-minisign.pub
ots verify SHA256SUMS.minisig.ots -f SHA256SUMS.minisig
sha256sum -c SHA256SUMS
```

The bucket-root public key is convenient but is not an independent trust anchor. The
OpenTimestamps checks are evidence about when the bytes existed; they are not required to
reconstruct the files.

## Reference recovery utility

Install the independently packaged `riverhog-recover` utility and the official `age`
distribution, including `age-plugin-batchpass`. Point it at one downloaded archive directory
and a new output path:

```sh
riverhog-recover ./archives/ARCHIVE_ID ./recovered-collection
```

The utility prompts once for the archive passphrase, passes it to the official age plugin
over an inherited file descriptor, verifies any downloaded ciphertext inventory, decrypts
one bounded archive object at a time, reconstructs pack members and segments, verifies every
object and file size and SHA-256 digest, and publishes the output directory only after the
whole collection succeeds. It does not import Riverhog server code or read a Riverhog
database.

For an operator-controlled noninteractive recovery, put the passphrase in a permission-
restricted file and pass `--passphrase-file PATH`. Remove that file after recovery.

## Manual interpretation

Decrypt the portable manifest with the same passphrase:

```sh
age --decrypt -o manifest.yml manifest.yml.age
```

`manifest.yml` uses `collection-archive-manifest/v2`. Its `objects` entries give each
decrypted archive object's kind, byte count, and SHA-256 digest. Its `files` entries give
each logical relative path, final byte count and digest, and an ordered object mapping:

- `file` is one complete file;
- `segment` entries are concatenated in increasing file-offset order;
- `pack` is a tar stream, and the mapping's `member` names the exact logical file.

Decrypt individual objects with standard tooling and verify their manifest digest before
using them:

```sh
age --decrypt -o data-000000 objects/data-000000.age
sha256sum data-000000
tar -xOf data-000000 MEMBER > recovered-file  # pack objects only
```

Finally verify every reconstructed file's exact byte count and SHA-256 digest against the
manifest. The encrypted manifest, archive data objects, and passphrase are sufficient;
`metadata.yml.age`, Riverhog's catalog, and Riverhog itself are not required for byte-exact
reconstruction once the correct opaque archive directory has been identified.
