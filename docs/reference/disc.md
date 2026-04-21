# Disc Format Reference

This document is normative for any ISO returned by `GET /v1/images/{image_id}/iso`.

## Commitment

- the disc planner must budget every byte that will land on the image: encrypted payloads, encrypted sidecars, encrypted collection manifests, encrypted OpenTimestamps proofs, the encrypted disc manifest, `README.md`, and ISO filesystem overhead
- `README.md` is the only plaintext leaf file on the disc
- every other leaf file is individually encrypted with `age-plugin-batchpass`
- on-disc filenames are generic; canonical collection paths live only inside decrypted YAML
- any collection represented on a disc, whether whole or partial, must also contribute its whole collection hash manifest and its `.ots` proof

## Canonical Root Layout

```text
README.md
DISC.yml.age
files/
  000001.age
  000001.yml.age
  000002.001.age
  000002.001.yml.age
collections/
  000001.yml.age
  000001.ots.age
```

Rules:

- `files/*.age` are encrypted payload objects
- `files/*.yml.age` are encrypted sidecar YAML files for the payload object with the same stem
- `collections/*.yml.age` decrypt to the collection hash manifest for one represented collection
- `collections/*.ots.age` decrypt to the OpenTimestamps proof for that collection hash manifest
- split files use `NNNNNN.PPP` stems, where `PP` is the 1-based part index on that image

## Disc Manifest

`DISC.yml.age` decrypts to minimalist YAML with schema `disc-manifest/v1`.

```yaml
schema: disc-manifest/v1
image:
  id: img_2026-04-20_01
  volume_id: ARC-IMG-20260420-01
collections:
  - id: docs
    manifest: collections/000001.yml.age
    proof: collections/000001.ots.age
    files:
      - path: /tax/2022/invoice-123.pdf
        bytes: 21
        sha256: ...
        object: files/000001.age
        sidecar: files/000001.yml.age
      - path: /raw/video.mov
        bytes: 7340032000
        sha256: ...
        parts:
          count: 3
          present:
            - index: 2
              object: files/000014.002.age
              sidecar: files/000014.002.yml.age
```

Rules:

- `collections[].id + files[].path` is the canonical logical path
- whole files use `object` plus `sidecar`
- split files use `parts.count` plus `parts.present[]`
- `parts.present[]` lists only the parts physically present on this image

## Per-File Sidecar

Each `files/*.yml.age` decrypts to minimalist YAML with schema `file-sidecar/v1`.

```yaml
schema: file-sidecar/v1
collection: docs
path: /tax/2022/invoice-123.pdf
bytes: 21
sha256: ...
mode: 420
mtime: 1713614400
uid: 1000
gid: 1000
part:
  index: 2
  count: 3
```

Rules:

- `part` is omitted for unsplit files
- the sidecar must contain enough metadata to restore the file without the API

## Collection Artifacts

For every represented collection:

- the disc must include the whole collection hash manifest, not only the files present on that image
- the disc must include the corresponding OpenTimestamps proof file
- both are encrypted like any other non-README disc object

This lets a person or tool verify reconstructed files against the collection-level manifest after recovery.

## `arc-disc` Expectations

`arc-disc` should treat the API fetch manifest and the disc manifest as complementary:

- the fetch manifest says which logical files are needed and which copies may satisfy them
- the disc manifest says where this image stores the encrypted objects for those logical files
- the sidecar says how to restore metadata and, for split files, how each object participates in the full plaintext

Expected flow:

1. read the fetch manifest from the API
2. load and decrypt `DISC.yml.age` from the inserted disc
3. resolve each required logical file to one `object` or to `parts.present[]`
4. decrypt the payload object(s) and sidecar object(s)
5. concatenate plaintext parts in ascending `index` order when the file is split
6. verify the final plaintext against the expected `sha256`
7. upload the recovered plaintext back to the API

## Manual Recovery

Without `arc-disc`, the intended recovery path is:

1. read `README.md`
2. decrypt `DISC.yml.age`
3. locate the desired collection id and file path
4. decrypt the referenced payload object and its sidecar
5. if the file is split, gather every disc whose `DISC.yml.age` lists that same collection id and file path, then concatenate decrypted plaintext parts in ascending `index` order
6. restore metadata from the sidecar and verify the resulting plaintext hash
7. decrypt the collection hash manifest and `.ots` proof to validate the reconstructed collection

If a collection spans multiple discs, the merge key is always `collection id + path`, never the generic on-disc object name.
