# Disc Format Reference

This document is normative for any ISO returned by `GET /v1/images/{image_id}/iso`.
Here `image_id` is the finalized image id in compact UTC basic form.
The machine-readable contract files live in `contracts/disc/`:

- `root-layout.json`
- `disc-manifest.schema.json`
- `file-sidecar.schema.json`
- `collection-hash-manifest.schema.json`

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
- no other leaf paths are valid contract output

## Disc Manifest

`DISC.yml.age` decrypts to minimalist YAML with schema `disc-manifest/v1`.

```yaml
schema: disc-manifest/v1
image:
  id: 20260420T040001Z
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
- `image.id` is the immutable finalized image id assigned when that image is explicitly finalized
- `collections[]` and each `files[]` list are lexically sorted for deterministic images
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

## Collection Hash Manifest

Each `collections/*.yml.age` decrypts to YAML with schema `collection-hash-manifest/v1`.

```yaml
schema: collection-hash-manifest/v1
collection: docs
generated_at: 2026-04-20T12:00:00Z
tree:
  sha256: ...
  total_bytes: 54
directories:
  - letters
  - tax
  - tax/2022
files:
  - relative_path: letters/cover.txt
    size_bytes: 13
    sha256: ...
  - relative_path: tax/2022/invoice-123.pdf
    size_bytes: 21
    sha256: ...
```

Rules:

- the manifest covers the whole represented collection, not only the files present on the current disc
- `directories[]` and `files[].relative_path` are lexically sorted for deterministic media
- `tree.total_bytes` is the sum of every `files[].size_bytes`

## Collection Artifacts

For every represented collection:

- the disc must include the whole collection hash manifest, not only the files present on that image
- the disc must include the corresponding OpenTimestamps proof file
- both are encrypted like any other non-README disc object

This lets a person or tool verify reconstructed files against the collection-level manifest after recovery.

## `arc-disc` Expectations

Automated multipart recovery uses the fetch manifest as its recovery contract.

- the fetch manifest is the source of truth for automated recovery orchestration
- multipart logical files include part-level recovery hints in the fetch manifest
- `DISC.yml.age` is the durable media contract for manual recovery, validation, and offline
  inspection
- the sidecar says how to restore metadata and, for split files, how each object participates in the
  full plaintext
- resumable recovery state for partially uploaded logical files is managed by the server-side fetch manifest
- fetch copy hints name the exact payload object to read plus the raw encrypted recovery-byte digest and length expected
  from that object
- `arc-disc` does not own decryption or final logical-file hash validation; the server does that behind the upload
  resource as needed
- any temporary buffering used during recovery is an internal implementation detail
- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk and the manifest returns to
  `waiting_media`
- `arc-disc` reports precise progress for the current file and the whole manifest throughout recovery and upload

Expected multipart flow:

1. read the fetch manifest from the API
2. determine which disc is needed next from the manifest's part-level recovery hints
3. prompt for successive disc insertions until every required part has been recovered
4. read the hinted payload object(s) from each disc
5. stream the raw encrypted payload-object bytes directly into the entry's resumable upload resource
6. if the logical file is split, continue streaming successive parts in ascending `index` order into that same upload
   resource
7. let the server decrypt, validate, and materialize the logical file as needed
8. rely on the manifest's resumable upload state if the process is interrupted before completion

## Guided Burn Sessions

`arc-disc burn` is the guided workflow for clearing the current finalized-image burn backlog.

- the burn backlog includes ready provisional candidates plus finalized images whose required copy backlog is not yet
  complete
- the session selects the fullest ready backlog item first
- if that item is still provisional, `arc-disc burn` finalizes it before continuing
- the session downloads and stages the image ISO locally before burn work
- the staged ISO is verified before burn work continues
- if the staged ISO is missing or no longer matches the last verified staged copy, `arc-disc burn` downloads it again
- one physical copy is burned and burned-media-verified at a time
- `arc-disc burn` prints the exact label text plus storage guidance before copy registration
- Riverhog does not register the copy, associate that generated `copy_id` with that physical disc, or count the copy
  toward coverage until the operator explicitly confirms that the disc is labeled
- if the session stops after burning or burned-media verification but before label confirmation, a later run first asks
  whether that unlabeled disc is still available
- if that unlabeled disc is still available, the session resumes from the earliest unfinished local checkpoint for that
  copy, including burned-media verification when needed
- if that unlabeled disc is no longer available, the local checkpoint is discarded and the copy is burned again as a
  replacement
- after label confirmation, `arc-disc burn` records the storage location, registers the generated copy id, and marks the
  copy verified before moving on

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
