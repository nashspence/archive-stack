# CLI Design

The implementation and executable tests are the source of truth for the CLI
contract. This note records the intended visual and command-design direction so
future changes stay consistent.

## Command Shape

Riverhog is collection-first:

- `riverhog collection ...` owns collection catalog, detail, file listing, and
  upload session controls.
- `riverhog hot ...` owns pinned hot-storage sets and fetch status.

Djdan is optical-media-first:

- `djdan burn` clears the burn backlog.
- `djdan fetch` clears the fetch backlog from discs.
- `djdan image ...` owns finalized images, planner output, downloads, and image
  rebuild work.
- `djdan disc ...` owns burned disc/copy catalog and state changes.

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

## JSON Output

`--json` is for machines:

- Emit clean JSON only on stdout.
- Prefer exact API payloads for commands that map one-to-one to an endpoint.
- Keep progress, prompts, and warnings on stderr.
- Do not include Rich formatting, table labels, or human summary text.
