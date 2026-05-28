# Disc Format Reference

This document is normative for any ISO returned by `GET /v1/images/{image_id}/iso`.
Here `image_id` is the finalized image id in compact UTC basic form.
The machine-readable contract files live in `contracts/disc/`:

- `root-layout.json`
- `disc-manifest.schema.json`
- `file-sidecar.schema.json`
- `collection-manifest.schema.json`

## Commitment

- the disc planner must budget every byte that will land on the image: encrypted payloads, encrypted sidecars, encrypted collection manifests, encrypted OpenTimestamps proofs, the encrypted disc manifest, `README.md`, and ISO filesystem overhead
- the disc planner must budget every collection's encrypted manifest and encrypted OpenTimestamps proof on every image
  that contains any part of that collection
- the disc planner may reorder collection pieces across candidate images to improve packing
- files are never voluntarily split; file parts only exist when a single file cannot fit on one image
- collections that require multiple images are split only as required and are not split further for packing
- collections that could fit on one image may be split once, by whole files, to improve packing
- whether a collection could fit on one image is evaluated against the complete collection, not only its currently
  unburned remainder
- each such optionally split collection may appear on at most two candidate images, and each candidate image may contain
  at most one optionally split collection
- `README.md` is the only plaintext leaf file on the disc
- every other leaf file is individually encrypted with `age-plugin-batchpass`
- on-disc filenames are generic; canonical collection paths live only inside decrypted YAML
- any collection represented on a disc, whether whole or partial, must also contribute its whole collection manifest and its `.ots` proof

## Planner Sizing

Provisional candidates are planned against the configured target byte size before
any ISO is exposed as ready. The planner budgets:

- each encrypted leaf as the age stream-size estimate plus a 256 byte age-header
  reserve and a 2048 byte ISO leaf-metadata reserve
- each represented file part with an additional 256 byte `DISC.yml` manifest
  entry reserve
- each collection manifest and `.ots` proof on every candidate that contains
  any part of that collection
- a 4 MiB base candidate metadata reserve

After materialization, Riverhog runs `xorriso -print-size` against the actual
image root. A candidate is reported as ISO-ready only if that measured ISO size
is at or below `RIVERHOG_PLANNER_DISC_TARGET_BYTES` and satisfies the configured
minimum fill rule or saturation override.

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
- `collections/*.yml.age` decrypt to the collection manifest for one represented collection
- `collections/*.ots.age` decrypt to the OpenTimestamps proof for that collection manifest
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
- `mode`, `mtime`, `uid`, and `gid` are optional and omitted when Riverhog does not know them
- the sidecar must contain enough metadata to identify, order, and verify the file without the API

## Collection Manifest

Each `collections/*.yml.age` decrypts to YAML with schema `collection-manifest/v1`.
This exact manifest and its matching OpenTimestamps proof are stored as sibling Standard S3
objects beside the collection tar and on every disc that represents any part of the collection.

```yaml
schema: collection-manifest/v1
collection: docs
tree:
  sha256: ...
  total_bytes: 54
files:
  - path: letters/cover.txt
    bytes: 13
    sha256: ...
  - path: tax/2022/invoice-123.pdf
    bytes: 21
    sha256: ...
```

Rules:

- the manifest covers the whole represented collection, not only the files present on the current disc
- `files[].path` is lexically sorted for deterministic media
- `tree.total_bytes` is the sum of every `files[].bytes`

## Collection Artifacts

For every represented collection:

- the disc must include the whole collection manifest, not only the files present on that image
- the disc must include the corresponding OpenTimestamps proof file
- both are encrypted like any other non-README disc object

This lets a person or tool verify reconstructed files against the same collection-level manifest used
for the Glacier archive object set.

## `djdan` Expectations

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
- recovery-byte lengths are captured when a copy is registered, and missing recovery-byte digests are lazily
  backfilled from the finalized image root before a fetch manifest is returned; archived-only files do not need hot
  plaintext in order to publish or complete a fetch manifest
- `djdan` does not own decryption or final logical-file hash validation; the server does that behind the upload
  resource as needed
- any temporary buffering used during recovery is an internal implementation detail
- the default recovery reader supports mounted optical filesystems directly and raw optical devices through `xorriso`
- incomplete upload state expires after `INCOMPLETE_UPLOAD_TTL` since the last accepted chunk and the manifest returns to
  `waiting_media`
- `djdan` reports precise progress for the current file and the whole manifest throughout recovery and upload

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

`djdan burn` is the guided workflow for clearing the current finalized-image burn backlog.

- the burn backlog includes ready provisional candidates plus finalized images whose required copy backlog is not yet
  complete while at least one protected copy still exists or every generated copy is still pending local burn work
- if a finalized image loses all protected copies, Riverhog opens an
  `image_rebuild` recovery session and removes that image from the ordinary burn
  backlog until rebuild proceeds through the recovery-session flow
- historical `lost` or `damaged` copy records are not burned again in place; replacement work uses fresh generated
  `copy_id` values in state `needed` or `burning`
- the session selects the fullest ready backlog item first
- if that item is still provisional, `djdan burn` finalizes it before continuing
- the session downloads and stages the image ISO locally before burn work
- the staged ISO is verified before burn work continues
- the default burn backend uses `hdiutil burn` on macOS and `xorriso -as cdrecord` elsewhere
- when `--device` is omitted, `djdan burn` lets `hdiutil burn` select the system burner on macOS and uses `/dev/sr0`
  on Linux-style hosts
- on macOS, explicit `/dev/diskN` optical-device hints are validated with `diskutil`, then `djdan` still lets
  `hdiutil burn` select the system burner instead of passing an `hdiutil -device` value
- operators may force a native hdiutil target with a device value of `hdiutil:IOService:...` from
  `hdiutil burn -list`
- on macOS real burns use native `hdiutil burn -verifyburn -eject`; `djdan` passes the hdiutil progress output through
  directly, treats successful completion as burned-media verification, and ejects the disc for labeling
- other burn backends, and resume paths that still need independent verification, read back the staged ISO's byte length
  from the optical device and compare its SHA-256 to the staged ISO
- `djdan burn --simulate` uses native non-writing burn mode against the configured optical device for the next pending
  copy (`hdiutil burn -testburn` on macOS, xorriso cdrecord-emulation `-dummy` elsewhere), then exits without
  burned-media verification, label confirmation, copy registration, or local burn checkpoint changes
- macOS native `-testburn` support depends on the drive/media family; BD-R media may be burnable while still not
  exposing native test-burn support through DiscRecording
- if the staged ISO is missing or no longer matches the last verified staged copy, `djdan burn` downloads it again
- one physical copy is burned and burned-media-verified at a time
- after burned-media verification, `djdan burn` asks Riverhog to send the best-effort `images.copy_label_needed`
  operator notification, then prints the exact label text plus storage guidance before copy registration
- Riverhog does not register the copy, associate that generated `copy_id` with that physical disc, or count the copy
  toward coverage until the operator explicitly confirms that the disc is labeled
- if the session stops after burning or burned-media verification but before label confirmation, a later run first asks
  whether that unlabeled disc is still available
- if that unlabeled disc is still available, the session resumes from the earliest unfinished local checkpoint for that
  copy, including burned-media verification when needed
- if that unlabeled disc is no longer available, the local checkpoint is discarded and the copy is burned again as a
  replacement
- after label confirmation, `djdan burn` records the storage location, registers the generated copy id, and marks the
  copy verified before moving on; Riverhog builds the heavier per-file recovery index for that copy asynchronously
- if no ordinary burn backlog remains but one or more images are waiting on
  `image_rebuild` work, `djdan burn` reports those recovery sessions instead
  of treating them as ordinary replacement burns

## Recovery Sessions

`djdan recover` is the guided workflow for `image_rebuild` recovery sessions
after one or more finalized images lose all protected copies.

- without a session id, `djdan recover` lists active image-rebuild recovery
  sessions and the finalized images attached to each one
- with a session id in `pending_approval`, `djdan recover` approves the estimated restore cost and exits after the
  restore request is submitted
- recovery-session readiness is driven by archive-store restore status, not only by the operator-facing latency
  estimate
- AWS S3 Glacier Deep Archive Bulk recovery should be expected to wait roughly
  48 hours; Riverhog polls S3 restore state and uses the configured ready TTL as
  the temporary-copy window once S3 reports the archive object restored
- with a session id in `ready`, `djdan recover` stages every still-needed
  rebuilt image ISO in that session before burn work starts so a later retry can
  resume from local artifacts
- ready sessions stage ISO bytes rebuilt from restored collection archives and
  persisted image coverage metadata
- if the restore window expires after local staging succeeded, `djdan recover` can still resume from the staged ISO
  artifacts already on disk
- recovery burns reuse the same local checkpoint behavior as `djdan burn`, including resume from unfinished
  burned-media verification or label confirmation
- when the recovery session finishes, Riverhog marks the session completed,
  records archive restore cleanup or lifecycle handoff for the collection
  archives, and deletes the staged ISO artifacts for the rebuilt images
  immediately

## Manual Recovery

Without `djdan`, the intended recovery path is:

1. read `README.md`
2. decrypt `DISC.yml.age`
3. locate the desired collection id and file path
4. decrypt the referenced payload object and its sidecar
5. if the file is split, gather every disc whose `DISC.yml.age` lists that same collection id and file path, then concatenate decrypted plaintext parts in ascending `index` order
6. restore metadata from the sidecar and verify the resulting plaintext hash
7. decrypt the collection manifest and `.ots` proof to validate the reconstructed collection

If a collection spans multiple discs, the merge key is always `collection id + path`, never the generic on-disc object name.
