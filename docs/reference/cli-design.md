# CLI Design

The implementation and executable tests are the source of truth for the CLI
contract. This note records the intended visual and command-design direction so
future changes stay consistent.

## Command Shape

Riverhog is collection-first:

- `riverhog collection ...` owns collection catalog, detail, file listing, and
  upload session controls.
- `riverhog hot ...` owns named fetch manifests, hot-file eviction, and
  fetch-scoped archive materialization.
- `riverhog hot fetch show` is the bounded preflight/progress view.
- `riverhog hot fetch files` is the paged, searchable, sortable selected-file
  drill-down for a fetch.
- `riverhog hot fetch cancel` is the single cancellation path for active
  fetches, whether they were queued for optical media or archive materialization.

Djdan is optical-media-first:

- `djdan burn` clears the burn backlog.
- `djdan fetch` clears the fetch backlog from discs.
- `djdan image ...` owns finalized images, planner output, downloads, and image
  disc rebuild archive restores.
- `djdan disc ...` owns burned disc catalog and state changes.

Avoid compatibility aliases. Prefer a small canonical surface over a broad one.

## Human Output

Human output should be calm, dense, and boring in the best way:

- Use Rich tables for list views.
- Use stable key/value labels for detail views and workflow summaries.
- Keep table columns few and operational: id, state, coverage, bytes, location,
  and next action.
- Do not print raw JSON in human mode.
- Avoid decorative panels, banners, color-heavy output, and prose explanations.
- Rich output must degrade to plain, readable text when Rich is unavailable,
  when `TERM=dumb`, or when `RIVERHOG_CLI_PLAIN=1`.

Visual emphasis should be sparse and role-based:

- Field names and table column headers use bold `#c0ad6c`.
- Entity ids in tables and detail views use bold `#8ec9cc`.
- Partial coverage values use bold `#ff8933`.
- Upload progress uses the same roles: labels use the field color, collection ids
  use the entity color, and partial/failed/retry states use the attention color.
  Active byte-transfer progress must be derived only from bytes accepted by the
  upload endpoint; do not add polling, rescans, or per-chunk work proportional
  to the number of files.

## JSON Output

`--json` is for machines:

- Emit clean JSON only on stdout.
- Prefer exact API payloads for commands that map one-to-one to an endpoint.
- Keep list-command JSON compact; detail commands can expose richer nested
  payloads.
- Keep progress, prompts, and warnings on stderr.
- Do not include Rich formatting, table labels, or human summary text.

## Process Audit Views

Read-only process views must make active work, failures, and terminal cleanup or
result state visible without loading unbounded history in a CLI process.
Paging, filtering, and sorting belong to the service or API that owns the
durable state. Aggregate fields used for list sorting or filtering come from
transactionally maintained summary data rather than read-time scans.

The public CLI family uses these owning views:

- `riverhog hot fetch list|show|files` reads fetch-keyed summary and file
  projections; `show` returns bounded entry and archive-restore previews.
- `munchy job list` pages indexed runner job summaries; `job show` is keyed by
  one exact job id.
- `jeb status` uses bounded active-attempt and recent-failure pages plus state
  counts; `jeb batches` pages indexed batch-attempt summaries. Source backlog
  inspection is current filesystem state, not history, and can be skipped with
  `--no-backlog`.
- `djdan image plan|list` and `djdan disc list` use server-paged planner, image,
  and disc projections. `djdan disc rebuild list` uses the archive-restore
  API's `terminal=active` database filter by default and never composes active
  history by fetching state pages in the CLI.

Use the shared `terminal=active|terminal|all` vocabulary where a process list
needs lifecycle scope. An exact state filter remains available for targeted
audit work.

## Dry Runs

Mutating commands must provide `--dry-run` when the action is bulk,
destructive, expensive, asynchronous, externally side-effecting, selector-based,
or selected by config/route logic rather than by one exact object id. This
includes uploads, evictions, cleanup, queued background work, runner/device/archive
fanout, and commands whose target set is discovered from selectors or local
filesystem scans.

Small direct state changes do not need ceremonial dry-runs when a clear
`show`/`status`/`list` command already answers the operator question. Examples
include one-id pause/resume/cancel operations and simple metadata edits. If a
state change has fanout, cleanup, deletion, expensive transfer, or non-obvious
selection, it crosses the threshold and needs a dry-run.

The dry-run contract is:

- The CLI flag is named `--dry-run`.
- Public API operations expose `dry_run: true` on the same operation endpoint
  when the operation is server-owned and that is practical. Local-only previews
  may remain CLI-side when the first mutation is a CLI-controlled local write or
  upload start.
- Dry-runs run the same validation and planning path as the real operation as
  far as possible without mutation.
- Dry-runs must not write durable state, upload, delete, enqueue jobs, acquire
  durable leases, send notifications, or persist preflight failures.
- Human output uses explicit `dry-run` or `would_*` language and includes the
  selected ids/files/bytes, destination or queued state, skipped or blocked
  reasons, and any would-be ids that can be computed without mutation.
- JSON output includes `dry_run: true` and a `status` such as `would_upload`,
  `would_evict`, `would_queue`, or `would_start`.
- Tests for dry-run behavior must prove the durable state that the real command
  would mutate is unchanged.
