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
- collections that require multiple images are split only as required unless saturation splitting is needed
- collections that could fit on one image may be split once, by whole files, to make an underfilled candidate ready
  without increasing total waiting candidate bytes even before saturation
- whether a collection could fit on one image is evaluated against the complete collection, not only its currently
  unburned remainder
- each image may contain at most one optionally split collection
- when waiting candidate bytes exceed `RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES`, the planner may add fair
  beneficial whole-file voluntary splits, including for collections that already required splitting, until enough
  candidates meet the minimum fill threshold
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
minimum fill rule.

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
  `queued_djdan`
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
- selected backlog items and blank-media prompts include the stored target media capacity, so a finalized image planned
  for older media settings still tells the operator which blank disc size is required
- if that item is still provisional, `djdan burn` finalizes it before continuing
- for copies that still need a physical burn, the session asks for blank media before ISO staging/download so an
  already-inserted disc can start burning as soon as the download and staged-ISO verification complete
- the staged ISO is verified before burn work continues; silent local verifier stages print periodic heartbeat messages
  because large images and images with many file entries can take several minutes to check
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
- finalized ISO downloads are generated streams; interrupted downloads discard the partial `.part` file and must be
  restarted from the beginning unless Riverhog later gains a file-backed ISO cache
- `djdan burn --simulate` uses native non-writing burn mode against the configured optical device for the next pending
  copy (`hdiutil burn -testburn` on macOS, xorriso cdrecord-emulation `-dummy` elsewhere), then exits without
  burned-media verification, label confirmation, copy registration, or local burn checkpoint changes
- macOS native `-testburn` support depends on the drive/media family; BD-R media may be burnable while still not
  exposing native test-burn support through DiscRecording
- if the staged ISO is missing or no longer matches the last verified staged copy, `djdan burn` downloads it again
- one physical copy is burned and burned-media-verified at a time
- burned-media verification failure is treated as a failed physical disc, not as a retryable verification result:
  `djdan` tells the operator to discard or destroy that disc, clears the local checkpoint for that copy, asks for a
  new blank disc, and burns the same generated copy id again
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
  copy verified before moving on; copy registration includes the per-file recovery index, and `djdan` reports that
  this can take time for images with many small files
- once an ordinary image has no pending burn copies or unfinished local verification checkpoints, `djdan burn` removes
  that image's staged ISO and local burn checkpoint data
- if no ordinary burn backlog remains but one or more images are waiting on
  `image_rebuild` work, `djdan burn` reports those recovery sessions instead
  of treating them as ordinary replacement burns

## Recovery Sessions

- `djdan disc rebuild COPY_ID --reason lost|damaged` marks a burned disc
  lost or damaged and shows the rebuild work needed to restore coverage
- `djdan disc rebuild list` lists active image-rebuild recovery sessions and
  the finalized images attached to each one
- `djdan disc rebuild show SESSION` shows restore readiness, attached images,
  and latest operator message
- `djdan disc rebuild pause SESSION` pauses active restore work when the
  operator is not ready to rebuild and burn replacement media
- `djdan disc rebuild resume SESSION` resumes a paused rebuild session
- while a session is still `restore_requested`, `djdan burn` exits cleanly and
  reports the recovery session that is blocking burn backlog
- once the session is `ready`, `djdan burn` stages and burns the replacement
  media automatically
- recovery-session readiness is driven by archive-store restore status, not only by the operator-facing latency
  estimate
- AWS S3 Glacier Deep Archive Bulk recovery should be expected to wait roughly
  48 hours; Riverhog polls S3 restore state and uses the configured ready TTL as
  the temporary-copy window once S3 reports the archive object restored
- for a ready session, `djdan burn` stages every still-needed rebuilt image ISO
  in that session before burn work starts so a later retry can resume from local
  artifacts
- ready sessions stage ISO bytes rebuilt from restored collection archives and
  persisted image coverage metadata
- if the restore window expires after local staging succeeded,
  `djdan burn` can still resume from the staged ISO artifacts already on disk
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
