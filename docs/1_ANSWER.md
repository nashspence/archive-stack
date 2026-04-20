The model I would use is:

A collection is the logical namespace.
A file is the operational unit for restore and eviction.
Hot storage is a cache/materialization layer, not the source of truth.
The database is the source of truth for both archival existence and hot presence.

That leads to a much simpler system shape.

Your biggest design tension is that collections are how people think, but files are how storage reality works. So let collections stay the user-facing grouping, while making hot/cold movement happen at file or subtree granularity underneath.

The most important shift is this:

Do not let users manage hot storage by mutating the hot directory tree.

Instead, make hot storage read-only and fully generated from metadata. Users ask for things to be materialized into hot, or released from hot, through an API or CLI. The filesystem view is just a projection.

A good mental model is three layers.

First, the catalog:

* collection
* original relative path
* file hash
* size
* encrypted archival locations
* which optical volumes contain it
* hot state
* pin state
* last accessed / last restored / last explicitly requested

Second, the hot object store:

* immutable files keyed by content hash
* no collection/path semantics here
* safe to make read-only except for the system itself

Third, the projected namespace:

* the collection directories users see
* built as symlinks, hardlinks, or a generated mount/view into the hot object store
* this is what looks like “hot storage”

With that split, “remove from hot” is no longer deleting files from a collection tree. It is just changing metadata so the projection excludes those paths, then garbage collecting unneeded hot objects later.

That solves your awkwardness around scanning a mirror tree to infer intent.

## What I would add

I would introduce the concept of a hot residency request, or simpler, a pin.

A pin says: keep this thing materialized in hot.

Pin targets can be:

* whole collection
* directory subtree within a collection
* single file

Then your system computes hot state as the union of active pins plus any policy-based temporary cache entries.

That gives you a very small surface area:

* “restore this file/dir/collection to hot”
* “release this file/dir/collection from hot”
* “show what is pinned”
* “show what is only cached opportunistically”
* “show what optical media contains this”

This can be done in a minimal web UI and a slightly more powerful CLI, without becoming a file browser.

## Why this is better than direct deletion

If users delete directly from the hot tree, you immediately get ambiguity:

* did they mean “evict from cache” or “delete from the archive namespace”?
* did they delete a symlink only?
* what if the tree is partial?
* what if part of a directory is still needed because another request depends on it?

If instead they say “release collection X/path/prefix Y from hot”, intent is explicit. Then the system:

* removes the pin
* updates the projected namespace
* later reclaims unpinned hot blobs by GC

That is much less jarring and much safer.

## How to keep the UI minimal

You do not need a file browser. You need search plus actions.

A very small UI can support:

* global search box
* results grouped by collection/path
* for each result:

  * available on hot: yes/no
  * available on optical: disc IDs / locations
  * action: pin to hot
  * action: release from hot
* collection detail page:

  * summary only
  * counts, sizes, hot coverage, disc coverage
  * maybe a few path-prefix restore/release actions

For more precise operations, use a CLI companion tool.

For example:

* search "invoice 2021 pdf"
* restore collection-a:/docs/finance/invoice-123.pdf
* restore collection-a:/photos/trip-2020/
* release collection-a:/raw/video/
* status collection-a

That keeps the web app minimal while still allowing file-level control.

I would strongly prefer the precise restore/evict workflow to live in the CLI, not the web UI. The web UI can still expose search and coarse actions.

## How the hot tree should work

I would not store collection-shaped files directly as the canonical hot layout. I would store hot bytes in a content-addressed store, then project collection paths into it.

For example:

* `/hot/objects/ab/cd/<sha256>`
* `/hot/view/<collection>/original/path/file.ext -> ../../../objects/ab/cd/<sha256>`

Or a generated read-only mount with equivalent behavior.

That buys you a lot:

* deduplication if identical files occur across collections
* restore of one file does not require reconstructing an entire collection subtree physically
* eviction becomes unlinking projections plus eventual GC of unused objects
* hot storage can stay immutable at the data layer

You said you prefer hot storage read-only on disk. This fits that well. The “view” can be treated as read-only for users, while the service itself updates it atomically.

## How restore should work

When a restore is requested for a file/path/collection:

1. Look up which disc copies contain the needed files.
2. Generate a restore manifest.
3. The companion tool reads the indicated discs, decrypts the manifest/files, and writes recovered files into an ingest area on the server or uploads them back.
4. Server verifies hashes.
5. Server places verified bytes into the hot object store.
6. Server updates projections for the requested paths.
7. Server marks those paths pinned if the request was explicit.

That is important: explicit restore should usually imply pinning, otherwise the cache may evict the file immediately after recovery.

## How eviction should work

I would separate “release” from “delete bytes now”.

Release means:

* remove explicit pin for target
* remove projected paths from hot view if no other pin/policy requires them
* mark underlying hot objects as GC-eligible if no remaining references

Then a GC job can safely delete truly unreferenced blobs from the hot object store.

This means you never need a “scan what was manually deleted” endpoint.

## Recommended state model

At the file level, I would track at least:

* archived_status: not_archived / planned / archived / archived_verified
* hot_status: absent / materializing / present / corrupt
* pin_mode: none / explicit / policy
* logical path: collection_id + relative_path
* content hash
* hot object hash pointer
* restore provenance: which disc copy was used last
* refcounts or dependency counts for projections/pins

At the collection level, keep aggregate summaries only.

A simple but powerful distinction is:

* explicit pins: user asked for it
* policy residency: system keeps it because it is recent/frequently used or because planner/burn workflow still needs it
* transient: being restored right now

Then hot coverage becomes understandable.

## How uploads/closing should fit

Your staging → close → hot + planner flow is fine.

I would model it as:

* staging area is mutable
* close operation freezes the collection snapshot
* closed collection gets cataloged fully
* closed collection is projected into hot immediately
* planner works from cataloged file list, not from ad hoc disk scans
* once optical copies exist and policy allows, parts of the collection can later be released from hot without changing the logical collection

That last point matters: a collection should not need to be either “in hot” or “out of hot”. It should have partial hot residency.

That seems to match what you want.

## How to specify release targets without file browsing

This is the key usability question.

I would support three selector forms everywhere, both API and CLI:

* exact file path
* directory prefix
* collection ID

Examples:

* `collection=photos-2024`
* `collection=photos-2024, prefix=/raw/`
* `collection=docs, path=/taxes/2022/return.pdf`

That is enough for precise control without a browser.

You can also support search-result-based actions:

* search for a file globally
* click “restore to hot” or “release from hot”

If you want one more ergonomic feature, add saved selectors:

* “working set: taxes”
* “working set: recent family photos”

But that may be more than you need.

## A better version of your “re-add-this-to-hot” idea

Your idea is good, but I would make it an import verification path, not the primary restore UX.

Have a designated recover/import directory where the companion tool or user can place files recovered from discs. Then an endpoint can:

* hash them
* match against known catalog entries by collection/path and/or hash
* place them into hot object store
* update projections
* optionally pin them

This is useful as a fallback and for manual workflows.

But I would not rely on users dropping arbitrary files there as the main mechanism. The main mechanism should be “request materialization by selector”; then the companion tool can fulfill the manifest.

That keeps intent clear and prevents accidental reintroduction of wrong versions or duplicate paths.

## Optical-specific recommendation

Since files may exist on multiple discs and collections may be split, I would treat restore planning as a set-cover problem over files requested.

Given a target selector, compute:

* all files required
* all disc copies that satisfy them
* best disc set minimizing swaps / number of discs / estimated read effort

Then the companion tool can say:

* insert disc BR-014
* read files A, B, C
* insert disc BR-022
* read files D, E

That is much nicer than restoring whole collection partitions.

## A small but useful concept: coverage

For each collection, track:

* total bytes
* bytes archived
* bytes hot
* bytes only on optical
* bytes not yet burned

For each directory prefix, optionally compute this on demand.

Then the UI can stay summary-oriented:

* collection exists
* partly hot
* fully archived on discs X, Y, Z
* 12 files / 1.2 GB currently pinned in hot

That gives users enough situational awareness without any browsing UI.

## What I would not do

I would avoid:

* letting users mutate hot paths directly
* a writable symlink mirror with delete-only permissions
* scan-to-infer-what-the-user-meant endpoints
* making collection the restore/evict unit
* treating “in hot” as a collection-wide boolean

All of those become awkward once partial restores matter.

## A concrete minimal API shape

Something like this would be enough:

* `POST /collections/close`
* `GET /search?q=...`
* `GET /collections/{id}`
* `GET /collections/{id}/coverage`
* `POST /hot/pin`
* `POST /hot/release`
* `GET /hot/pins`
* `POST /restore-plans`
* `GET /restore-plans/{id}`
* `POST /imports/verify`
* `POST /gc/run` or background GC

Where `/hot/pin` and `/hot/release` accept selectors like:

* collection_id
* prefix
* path

And `/restore-plans` returns:

* requested selector
* required files
* candidate discs
* optimal disc sequence
* manifest for companion tool

## A naming suggestion

“Collection” is okay as the logical unit, but you may want to distinguish these concepts explicitly:

* Collection: user-visible namespace
* Slice or Partition: planner-produced optical grouping
* Volume: actual burned disc instance
* Pin: desired hot residency
* Materialization: current hot realization of a selector

Those names make the architecture easier to reason about.

## My recommendation in one sentence

Make hot storage an immutable, content-addressed cache plus a generated read-only collection view, and manage partial restore/eviction through explicit file/prefix/collection pins rather than filesystem deletion.
